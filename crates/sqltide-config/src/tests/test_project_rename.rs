//! Project config edits for `sqt rename-project` keep every authored byte they do not change.

use crate::project_rename::{
    ProjectIdentityKeys, project_identity_keys, rename_project_text, toml_basic_string,
};

const AUTHORED: &str = "# Shop analytics project.\n\
name = \"shop\"   # the project identity\n\
default_target = \"dev\"\n\
\n\
[targets.dev]\n\
# Local development database.\n\
database = \"shop_dev\"\n";

#[test]
fn renames_in_place_and_keeps_comments_and_spacing() {
    let edited = rename_project_text(AUTHORED, "storefront", None).unwrap();
    assert_eq!(
        edited,
        AUTHORED.replace("name = \"shop\"", "name = \"storefront\"")
    );
}

#[test]
fn adds_the_consumer_group_identity_after_the_name_line() {
    let edited = rename_project_text(AUTHORED, "storefront", Some("shop")).unwrap();
    assert_eq!(
        edited,
        "# Shop analytics project.\n\
name = \"storefront\"   # the project identity\n\
# Keeps the Kafka consumer groups this project used before it was renamed.\n\
consumer_group_identity = \"shop\"\n\
default_target = \"dev\"\n\
\n\
[targets.dev]\n\
# Local development database.\n\
database = \"shop_dev\"\n"
    );
    assert_eq!(
        project_identity_keys(&edited).unwrap(),
        ProjectIdentityKeys {
            name: Some("storefront".to_owned()),
            consumer_group_identity: Some("shop".to_owned()),
        }
    );
}

#[test]
fn keeps_crlf_line_endings_and_a_final_line_without_newline() {
    let edited = rename_project_text("name = 'shop'\r\nx = 1\r\n", "store", Some("shop")).unwrap();
    assert_eq!(
        edited,
        "name = \"store\"\r\n# Keeps the Kafka consumer groups this project used before it was \
         renamed.\r\nconsumer_group_identity = \"shop\"\r\nx = 1\r\n"
    );
    let edited = rename_project_text("x = 1\nname = \"shop\"", "store", Some("shop")).unwrap();
    assert!(edited.ends_with("name = \"store\"\n# Keeps the Kafka consumer groups this project used before it was renamed.\nconsumer_group_identity = \"shop\"\n"));
}

#[test]
fn keeps_an_existing_consumer_group_identity() {
    let text = "name = \"storefront\"\nconsumer_group_identity = \"shop\"\n";
    assert_eq!(
        rename_project_text(text, "outlet", None).unwrap(),
        "name = \"outlet\"\nconsumer_group_identity = \"shop\"\n"
    );
    assert_eq!(
        rename_project_text(text, "outlet", Some("shop")).unwrap_err(),
        "project config already declares 'consumer_group_identity'"
    );
}

#[test]
fn refuses_documents_without_a_string_name() {
    assert!(rename_project_text("default_target = \"dev\"\n", "x", None).is_err());
    assert!(rename_project_text("name = 1\n", "x", None).is_err());
    assert!(rename_project_text("name = \n", "x", None).is_err());
}

#[test]
fn quotes_names_as_basic_strings() {
    assert_eq!(toml_basic_string("a\"b\\c\u{1}"), "\"a\\\"b\\\\c\\u0001\"");
}
