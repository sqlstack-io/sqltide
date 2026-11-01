//! Staged deployment steps, shadow identities, and rollout warnings for rebuild subtrees.

use std::collections::HashSet;

/// One desired object a deployment plan can stage.
pub struct PlanKey<'a> {
    /// The desired object type, such as `table` or `materialized_view`.
    pub object_type: &'a str,
    pub name: &'a str,
    /// Whether the object reads mutable side refs, so replay cannot be exact.
    pub uses_mutable_refs: bool,
}

/// A rebuild subtree as indexes into the plan's keys.
pub struct PlanSubtree {
    pub root: usize,
    pub affected: Vec<usize>,
}

/// One staged deployment step.
#[derive(Debug, PartialEq, Eq)]
pub struct Step {
    pub step_id: String,
    pub phase: &'static str,
    pub action: &'static str,
    pub root: usize,
    pub target: Option<usize>,
    pub physical_name: Option<String>,
}

/// The steps, deployment-suffixed shadow names, and `(root, target)` mutable-ref warnings.
#[derive(Debug, PartialEq, Eq)]
pub struct StagedDeployment {
    pub steps: Vec<Step>,
    pub shadow_objects: Vec<(usize, String)>,
    pub mutable_ref_warnings: Vec<(usize, usize)>,
}

/// The deployment-suffixed physical name of a logical object.
#[must_use]
pub fn deployment_physical_name(logical_name: &str, deployment_id: &str) -> String {
    format!("{logical_name}__{deployment_id}")
}

fn plan_action(object_type: &str) -> &'static str {
    match object_type {
        "materialized_view" => "plan_shadow_materialized_view",
        "view" => "plan_shadow_view",
        _ => "plan_shadow_table",
    }
}

/// Stage every subtree as plan, backfill, audit, and publish steps.
#[must_use]
pub fn stage_deployment(
    keys: &[PlanKey<'_>],
    subtrees: &[PlanSubtree],
    deployment_id: Option<&str>,
) -> StagedDeployment {
    let mut shadow_objects = Vec::new();
    if let Some(deployment_id) = deployment_id {
        let mut seen = HashSet::new();
        for &target in subtrees.iter().flat_map(|subtree| &subtree.affected) {
            if seen.insert(target) {
                shadow_objects.push((
                    target,
                    deployment_physical_name(keys[target].name, deployment_id),
                ));
            }
        }
    }
    let physical_name = |key: usize| {
        shadow_objects
            .iter()
            .find(|(shadow, _)| *shadow == key)
            .map(|(_, name)| name.clone())
    };
    let mut steps = Vec::new();
    let mut mutable_ref_warnings = Vec::new();
    for (position, subtree) in subtrees.iter().enumerate() {
        let number = position + 1;
        for &target in &subtree.affected {
            let key = &keys[target];
            steps.push(Step {
                step_id: format!("plan-{number}-{}-{}", key.object_type, key.name),
                phase: "plan",
                action: plan_action(key.object_type),
                root: subtree.root,
                target: Some(target),
                physical_name: physical_name(target),
            });
            if key.uses_mutable_refs {
                mutable_ref_warnings.push((subtree.root, target));
            }
        }
        let root_name = keys[subtree.root].name;
        for (phase, action) in [
            ("backfill", "backfill_subtree"),
            ("audit", "audit_subtree"),
            ("publish", "publish_subtree"),
        ] {
            steps.push(Step {
                step_id: format!("{phase}-{number}-{root_name}"),
                phase,
                action,
                root: subtree.root,
                target: None,
                physical_name: physical_name(subtree.root),
            });
        }
    }
    StagedDeployment {
        steps,
        shadow_objects,
        mutable_ref_warnings,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn keys() -> Vec<PlanKey<'static>> {
        vec![
            PlanKey {
                object_type: "table",
                name: "orders",
                uses_mutable_refs: false,
            },
            PlanKey {
                object_type: "materialized_view",
                name: "orders_mv",
                uses_mutable_refs: true,
            },
            PlanKey {
                object_type: "view",
                name: "orders_v",
                uses_mutable_refs: false,
            },
        ]
    }

    #[test]
    fn stages_each_subtree_and_shares_shadow_names_across_subtrees() {
        let subtrees = [
            PlanSubtree {
                root: 1,
                affected: vec![1, 0],
            },
            PlanSubtree {
                root: 2,
                affected: vec![2, 0],
            },
        ];
        let staged = stage_deployment(&keys(), &subtrees, Some("20260101T000000Z_ab"));
        assert_eq!(
            staged.shadow_objects,
            [
                (1, "orders_mv__20260101T000000Z_ab".to_owned()),
                (0, "orders__20260101T000000Z_ab".to_owned()),
                (2, "orders_v__20260101T000000Z_ab".to_owned()),
            ]
        );
        let ids: Vec<&str> = staged
            .steps
            .iter()
            .map(|step| step.step_id.as_str())
            .collect();
        assert_eq!(
            ids,
            [
                "plan-1-materialized_view-orders_mv",
                "plan-1-table-orders",
                "backfill-1-orders_mv",
                "audit-1-orders_mv",
                "publish-1-orders_mv",
                "plan-2-view-orders_v",
                "plan-2-table-orders",
                "backfill-2-orders_v",
                "audit-2-orders_v",
                "publish-2-orders_v",
            ]
        );
        assert_eq!(staged.steps[0].action, "plan_shadow_materialized_view");
        assert_eq!(staged.steps[5].action, "plan_shadow_view");
        assert_eq!(
            staged.steps[6].physical_name.as_deref(),
            Some("orders__20260101T000000Z_ab")
        );
        assert_eq!(staged.mutable_ref_warnings, [(1, 1)]);
    }

    #[test]
    fn leaves_physical_names_unset_without_a_deployment() {
        let staged = stage_deployment(
            &keys(),
            &[PlanSubtree {
                root: 0,
                affected: vec![0],
            }],
            None,
        );
        assert!(staged.shadow_objects.is_empty());
        assert!(staged.steps.iter().all(|step| step.physical_name.is_none()));
    }
}
