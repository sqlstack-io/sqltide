//! Pipeline modes read before discovery, which skips files discovery reports later.

use std::fs;
use std::path::{Path, PathBuf};

use crate::pipelines::declares_virtual_pipeline;

/// Pipeline directory names and the bytes of their `pipeline.toml`.
type Pipelines<'case> = &'case [(&'case str, &'case [u8])];

fn project(name: &str, pipelines: Pipelines<'_>) -> PathBuf {
    let root: PathBuf = std::env::temp_dir()
        .join(format!("sqltide-config-{}-{name}", std::process::id()))
        .join("pipelines");
    let _ = fs::remove_dir_all(&root);
    for (pipeline, contents) in pipelines {
        let directory: PathBuf = root.join(pipeline);
        fs::create_dir_all(&directory).expect("the pipeline directory is created");
        fs::write(directory.join("pipeline.toml"), contents).expect("the config is written");
    }
    fs::create_dir_all(&root).expect("the pipelines root is created");
    root
}

#[test]
fn given_pipeline_configs_when_checking_for_virtual_mode_then_only_valid_virtual_configs_count() {
    let cases: [(&str, Pipelines<'_>, bool); 5] = [
        ("none", &[], false),
        (
            "direct",
            &[
                ("orders", b"mode = \"direct\"\n"),
                ("customers", b"name = \"customers\"\n"),
            ],
            false,
        ),
        (
            "virtual-after-invalid",
            &[
                ("orders", b"mode = \"virtual\n"),
                ("customers", b"\xffmode = \"virtual\"\n"),
                ("inventory", b"name = \"inventory\"\rmode = 'virtual'\r\n"),
            ],
            true,
        ),
        (
            "not-a-string",
            &[("orders", b"mode = [\"virtual\"]\n")],
            false,
        ),
        (
            "case-sensitive",
            &[("orders", b"mode = \"Virtual\"\n")],
            false,
        ),
    ];

    for (name, pipelines, expected) in cases {
        let root: PathBuf = project(name, pipelines);
        assert_eq!(declares_virtual_pipeline(&root), expected, "{name}");
        let _ = fs::remove_dir_all(root.parent().expect("the project directory"));
    }
    assert!(!declares_virtual_pipeline(Path::new(
        "/nonexistent/pipelines"
    )));
}
