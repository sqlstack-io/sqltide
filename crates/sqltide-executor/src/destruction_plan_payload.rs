//! The strict, versioned JSON envelope that stores a frozen destruction plan.

use serde::{Deserialize, Serialize};
use speedate::{DateTime, DateTimeConfig, MicrosecondsPrecisionOverflowBehavior, TimeConfig};
use strum::EnumMessage;

use crate::timestamps::{civil_from_days, days_from_civil};

/// The envelope version this build writes and reads.
pub const PAYLOAD_VERSION: i64 = 1;
const SECONDS_PER_MINUTE: i32 = 60;

/// A timestamp as written: wall-clock fields and the UTC offset in seconds when aware.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Timestamp {
    pub year: u16,
    pub month: u8,
    pub day: u8,
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
    pub microsecond: u32,
    pub utc_offset_seconds: Option<i32>,
}

impl Timestamp {
    /// ISO 8601 with microseconds only when present, `Z` for an exact UTC offset, and any other
    /// offset truncated to whole minutes, as plans have always been written.
    fn text(&self) -> String {
        let mut text = format!(
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
            self.year, self.month, self.day, self.hour, self.minute, self.second
        );
        if self.microsecond != 0 {
            text.push_str(&format!(".{:06}", self.microsecond));
        }
        match self.utc_offset_seconds {
            None => {}
            Some(0) => text.push('Z'),
            Some(offset) => {
                let sign: char = if offset < 0 { '-' } else { '+' };
                let minutes: i32 = offset.abs() / SECONDS_PER_MINUTE;
                text.push_str(&format!("{sign}{:02}:{:02}", minutes / 60, minutes % 60));
            }
        }
        text
    }

    /// Parse a timestamp the way the stored payload has always been read: ISO 8601 with a `T`,
    /// `t`, `_` or space separator, or Unix seconds read as UTC.
    fn parse(text: &str) -> Result<Self, String> {
        let config = DateTimeConfig {
            time_config: TimeConfig {
                microseconds_precision_overflow_behavior:
                    MicrosecondsPrecisionOverflowBehavior::Truncate,
                unix_timestamp_offset: Some(0),
            },
            ..DateTimeConfig::default()
        };
        let parsed: DateTime = DateTime::parse_bytes_with_config(text.as_bytes(), &config)
            .map_err(|error| {
                format!(
                    "Input should be a valid datetime, {}",
                    error.get_documentation().unwrap_or_default()
                )
            })?;
        if parsed.date.year == 0 {
            return Err("Input should be a valid datetime, year 0 is out of range".to_owned());
        }
        Ok(Self {
            year: parsed.date.year,
            month: parsed.date.month,
            day: parsed.date.day,
            hour: parsed.time.hour,
            minute: parsed.time.minute,
            second: parsed.time.second,
            microsecond: parsed.time.microsecond,
            utc_offset_seconds: parsed.time.tz_offset,
        })
    }

    /// The same instant in UTC. Rejects naive timestamps and instants outside years 1 to 9999.
    fn utc(&self, field: &str) -> Result<Self, PayloadError> {
        let offset: i32 = self
            .utc_offset_seconds
            .ok_or_else(|| PayloadError::Corrupt(format!("{field} must include a timezone")))?;
        let seconds: i64 = self.ordinal().0 - i64::from(offset);
        let (year, month, day) = civil_from_days(seconds.div_euclid(86_400));
        let year: u16 = u16::try_from(year)
            .ok()
            .filter(|year| (1..=9_999).contains(year))
            .ok_or_else(|| PayloadError::Corrupt(format!("{field} is out of range in UTC")))?;
        let clock: i64 = seconds.rem_euclid(86_400);
        Ok(Self {
            year,
            month: month as u8,
            day: day as u8,
            hour: (clock / 3_600) as u8,
            minute: (clock % 3_600 / 60) as u8,
            second: (clock % 60) as u8,
            microsecond: self.microsecond,
            utc_offset_seconds: Some(0),
        })
    }

    fn ordinal(&self) -> (i64, u32) {
        (
            days_from_civil(
                i64::from(self.year),
                i64::from(self.month),
                i64::from(self.day),
            ) * 86_400
                + i64::from(self.hour) * 3_600
                + i64::from(self.minute) * 60
                + i64::from(self.second),
            self.microsecond,
        )
    }
}

mod timestamp_text {
    use serde::{Deserialize, Deserializer, Serializer};

    use super::Timestamp;

    pub(super) fn serialize<S: Serializer>(
        value: &Timestamp,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&value.text())
    }

    pub(super) fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Timestamp, D::Error> {
        let text: String = String::deserialize(deserializer)?;
        Timestamp::parse(&text).map_err(serde::de::Error::custom)
    }
}

/// One relation the plan drops, with the evidence that it belongs to the plan.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RelationPayload {
    pub database: String,
    pub name: String,
    pub kind: RelationKind,
    pub exists: bool,
    pub total_bytes: Option<i128>,
    pub active_parts: Option<i128>,
    pub catalog_fingerprint: Option<String>,
    pub logical_names: Vec<String>,
    pub pipeline_names: Vec<String>,
    pub ownership: Vec<Ownership>,
    pub dependency_relation_names: Vec<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RelationKind {
    View,
    MaterializedView,
    Table,
    ManagedSource,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Ownership {
    CurrentManifest,
    VirtualPhysicalMapping,
    PublishedStableBinding,
    OwnershipLedger,
    HistoricalManifest,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlanOperation {
    DestroyPipelines,
    ResetTarget,
}

/// Every field of a frozen destruction plan, in stored order.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlanPayload {
    pub plan_id: String,
    pub operation: PlanOperation,
    pub target: String,
    pub database: String,
    pub metadata_database: String,
    pub requested_pipeline_names: Vec<String>,
    pub included_dependent_pipeline_names: Vec<String>,
    pub affected_pipeline_names: Vec<String>,
    pub affected_model_names: Vec<String>,
    pub affected_source_names: Vec<String>,
    pub relations: Vec<RelationPayload>,
    pub challenges: Vec<String>,
    pub preserves_sources: bool,
    pub preserves_replay_data: bool,
    pub manifest_fingerprint: String,
    pub plan_fingerprint: String,
    #[serde(with = "timestamp_text")]
    pub created_at: Timestamp,
    #[serde(with = "timestamp_text")]
    pub expires_at: Timestamp,
    #[serde(default)]
    pub relation_drop_size_limit: Option<i128>,
    #[serde(default)]
    pub relation_drop_size_server_limit: Option<i128>,
    #[serde(default)]
    pub relation_drop_size_override: Option<i128>,
    #[serde(default)]
    pub relation_drop_size_policy_observed: bool,
    #[serde(default)]
    pub include_orphans: bool,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Envelope {
    version: i64,
    plan: PlanPayload,
}

#[derive(Serialize)]
struct EnvelopeRef<'plan> {
    version: i64,
    plan: &'plan PlanPayload,
}

/// Why a stored plan cannot be loaded.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PayloadError {
    /// The JSON does not match the envelope; the text is the decoder's reason.
    Incompatible(String),
    /// The envelope is well formed but its contents are not a usable plan.
    Corrupt(String),
}

/// The plan in the current envelope, as compact JSON.
pub fn serialize_plan(plan: &PlanPayload) -> String {
    serde_json::to_string(&EnvelopeRef {
        version: PAYLOAD_VERSION,
        plan,
    })
    .unwrap_or_default()
}

/// Load a stored plan, rejecting other versions, unknown fields, wrong types, naive timestamps
/// and an expiry that is not after creation. Timestamps come back in UTC.
pub fn deserialize_plan(payload_json: &str) -> Result<PlanPayload, PayloadError> {
    // Decoding through a JSON value keeps the last of any duplicated keys, as stored plans have
    // always been read.
    let value: serde_json::Value = serde_json::from_str(payload_json)
        .map_err(|error| PayloadError::Incompatible(error.to_string()))?;
    let envelope: Envelope = serde_path_to_error::deserialize(value).map_err(|error| {
        let path: String = error.path().to_string();
        let reason: String = error.into_inner().to_string();
        PayloadError::Incompatible(if path == "." {
            reason
        } else {
            format!("{path}: {reason}")
        })
    })?;
    if envelope.version != PAYLOAD_VERSION {
        return Err(PayloadError::Corrupt(format!(
            "unsupported payload version {}; expected {PAYLOAD_VERSION}",
            envelope.version
        )));
    }
    let mut plan: PlanPayload = envelope.plan;
    let created_at: Timestamp = plan.created_at.utc("created_at")?;
    let expires_at: Timestamp = plan.expires_at.utc("expires_at")?;
    if expires_at.ordinal() <= created_at.ordinal() {
        return Err(PayloadError::Corrupt(
            "expires_at must be later than created_at".to_owned(),
        ));
    }
    plan.created_at = created_at;
    plan.expires_at = expires_at;
    Ok(plan)
}
