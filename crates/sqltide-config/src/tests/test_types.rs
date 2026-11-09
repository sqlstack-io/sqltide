pub(super) struct AccessPolicyErrorTestCase {
    pub(super) description: &'static str,
    pub(super) contents: &'static str,
    pub(super) expected_message: &'static str,
    pub(super) expected_line: usize,
    pub(super) expected_column: usize,
}
