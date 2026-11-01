use super::*;

fn names(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| (*value).to_owned()).collect()
}

fn model(name: &str, pipeline: &str, resources: &[(&str, RelationKind)]) -> Key {
    Key {
        name: name.to_owned(),
        source: false,
        pipeline: Some(pipeline.to_owned()),
        resources: resources
            .iter()
            .map(|(name, kind)| ((*name).to_owned(), *kind))
            .collect(),
    }
}

/// `raw_orders` (managed source) feeds `orders` (pipeline a), which feeds `order_totals`
/// (pipeline b); pipeline c reads `raw_orders` directly through `returns`.
fn project() -> Project {
    let mut keys = vec![
        Key {
            name: "raw_orders".to_owned(),
            source: true,
            pipeline: None,
            resources: vec![("raw_orders".to_owned(), RelationKind::ManagedSource)],
        },
        model(
            "orders",
            "a",
            &[
                ("orders", RelationKind::Table),
                ("orders_mv", RelationKind::MaterializedView),
            ],
        ),
        model(
            "order_totals",
            "b",
            &[("order_totals", RelationKind::Table)],
        ),
        model(
            "returns",
            "c",
            &[
                ("returns", RelationKind::View),
                ("_SQLTIDE_state", RelationKind::Table),
            ],
        ),
    ];
    keys[0]
        .resources
        .push(("_ſqltide_offsets".to_owned(), RelationKind::Table));
    Project {
        keys,
        upstream: vec![vec![], vec![0], vec![1], vec![0]],
        downstream: vec![vec![1, 3], vec![2], vec![], vec![]],
        pipelines: vec![
            ("a".to_owned(), Some(0)),
            ("b".to_owned(), None),
            ("c".to_owned(), None),
        ],
        sources: vec![0],
        models: vec![1, 2, 3],
    }
}

fn destroy(pipelines: &[&str], included: &[&str]) -> Request {
    Request {
        operation: Operation::DestroyPipelines,
        database: "analytics".to_owned(),
        pipeline_names: names(pipelines),
        included_dependent_pipeline_names: names(included),
        include_orphans: false,
    }
}

fn empty_inventory() -> Inventory {
    Inventory {
        deployments: Vec::new(),
        publish_events: Vec::new(),
    }
}

#[test]
fn requires_downstream_and_shared_source_pipelines() {
    let available = names(&["a", "b", "c"]);
    assert_eq!(
        resolve_selection(&project(), &destroy(&["b"], &[]), &available),
        Err(DestructionError::Dependency(names(&["a", "c"])))
    );
    assert_eq!(
        resolve_selection(&project(), &destroy(&["a"], &["b"]), &available),
        Err(DestructionError::Dependency(names(&["c"])))
    );
    assert_eq!(
        resolve_selection(&project(), &destroy(&["c"], &["b", "a"]), &available),
        Ok((names(&["c"]), names(&["b", "a"]), names(&["a", "b", "c"])))
    );
    assert_eq!(
        resolve_selection(&project(), &destroy(&["z", "a"], &[]), &available),
        Err(DestructionError::UnknownPipelines(names(&["z"])))
    );
}

#[test]
fn owns_project_resources_and_published_inventory_but_not_metadata() {
    let inventory = Inventory {
        deployments: vec![Deployment {
            deployment_id: "d1".to_owned(),
            mappings: vec![Mapping {
                database: None,
                object_type: "table".to_owned(),
                name: "order_totals".to_owned(),
                physical_name: "order_totals__d1".to_owned(),
                logical_model_name: "order_totals".to_owned(),
            }],
        }],
        publish_events: vec![PublishEvent {
            deployment_id: "d1".to_owned(),
            published_at: "2026-01-01".to_owned(),
            bindings: vec![Binding {
                database: "analytics".to_owned(),
                logical_name: "order_totals".to_owned(),
                physical_name: "order_totals__d1".to_owned(),
            }],
        }],
    };
    let scope = plan_scope(
        &project(),
        &destroy(&["b"], &[]),
        &names(&["b"]),
        &names(&["a", "b", "c"]),
        None,
        &[],
        &inventory,
    )
    .unwrap();
    assert_eq!(scope.affected_model_names, names(&["order_totals"]));
    assert_eq!(scope.affected_source_names, ["raw_orders"]);
    let owned: Vec<(&str, RelationKind, Vec<Ownership>)> = scope
        .owned
        .iter()
        .map(|relation| {
            (
                relation.name.as_str(),
                relation.kind,
                relation.ownership.iter().copied().collect(),
            )
        })
        .collect();
    assert_eq!(
        owned,
        [
            (
                "order_totals",
                RelationKind::View,
                vec![
                    Ownership::CurrentManifest,
                    Ownership::PublishedStableBinding
                ]
            ),
            (
                "order_totals__d1",
                RelationKind::Table,
                vec![Ownership::VirtualPhysicalMapping]
            ),
            (
                "raw_orders",
                RelationKind::ManagedSource,
                vec![Ownership::CurrentManifest]
            ),
        ]
    );
    assert_eq!(
        scope.owned[1].pipeline_names,
        BTreeSet::from(["b".to_owned()])
    );

    let reset = Request {
        operation: Operation::ResetTarget,
        ..destroy(&[], &[])
    };
    let scope = plan_scope(
        &project(),
        &reset,
        &names(&["a", "b", "c"]),
        &names(&["a", "b", "c"]),
        None,
        &[],
        &empty_inventory(),
    )
    .unwrap();
    let names: Vec<&str> = scope
        .owned
        .iter()
        .map(|relation| relation.name.as_str())
        .collect();
    assert_eq!(
        names,
        [
            "order_totals",
            "orders",
            "orders_mv",
            "raw_orders",
            "returns"
        ]
    );
    assert_eq!(scope.affected_source_names, ["raw_orders"]);
}

#[test]
fn refuses_inventory_outside_the_target_database() {
    let inventory = Inventory {
        deployments: vec![Deployment {
            deployment_id: "d1".to_owned(),
            mappings: vec![Mapping {
                database: Some("other".to_owned()),
                object_type: "table".to_owned(),
                name: "orders".to_owned(),
                physical_name: "orders__d1".to_owned(),
                logical_model_name: "orders".to_owned(),
            }],
        }],
        publish_events: Vec::new(),
    };
    let error = plan_scope(
        &project(),
        &destroy(&["a"], &["b", "c"]),
        &names(&["a", "b", "c"]),
        &names(&["a", "b", "c"]),
        None,
        &[],
        &inventory,
    );
    assert_eq!(
        error,
        Err(DestructionError::Resource(
            "Destruction cannot mutate recorded virtual resources outside the target database: \
             other.orders__d1"
                .to_owned()
        ))
    );
}

#[test]
fn takes_unpublished_history_only_for_selected_inactive_pipelines() {
    let resource = |name: &str, pipeline: &str| ManifestResource {
        resource_name: name.to_owned(),
        pipeline_name: pipeline.to_owned(),
        logical_name: format!("{name}_model"),
        logical_type: "model".to_owned(),
        resource_role: "target".to_owned(),
        resource_database: "analytics".to_owned(),
        resource_kind: "table".to_owned(),
    };
    let snapshot = Snapshot {
        absent: false,
        manifests: vec![
            Manifest {
                published_at: "2026-02-01".to_owned(),
                resources: vec![resource("kept", "live")],
            },
            Manifest {
                published_at: "2026-01-01".to_owned(),
                resources: vec![resource("old", "gone"), resource("kept", "gone")],
            },
        ],
    };
    let catalog = ["old", "kept"].map(|name| CatalogRelation {
        name: name.to_owned(),
        engine: "MergeTree".to_owned(),
        ownership_generation: None,
        dependency_names: Vec::new(),
    });
    let scope = plan_scope(
        &project(),
        &destroy(&["gone"], &[]),
        &names(&["gone"]),
        &names(&["a", "b", "c"]),
        Some(&snapshot),
        &catalog,
        &empty_inventory(),
    )
    .unwrap();
    let owned: Vec<&str> = scope
        .owned
        .iter()
        .map(|relation| relation.name.as_str())
        .collect();
    assert_eq!(owned, ["old"]);
    assert_eq!(scope.affected_model_names, ["old_model"]);
    assert_eq!(
        available_pipeline_names(&names(&["a"]), Some(&snapshot)),
        names(&["a", "gone", "live"])
    );
    assert_eq!(
        inactive_pipelines(&names(&["live"]), &snapshot),
        [InactivePipeline {
            name: "gone".to_owned(),
            model_count: 1,
            resource_count: 1,
            last_published_at: "2026-01-01".to_owned(),
        }]
    );
}

#[test]
fn matches_python_casefold_for_the_metadata_prefix() {
    assert!(is_metadata_relation("_SQLTIDE_state"));
    assert!(is_metadata_relation("_ſqltide_"));
    assert!(!is_metadata_relation("_sqltide"));
    assert!(!is_metadata_relation("x_sqltide_"));
    assert!(!is_metadata_relation("_sqltİde_"));
}

#[test]
fn reports_oversized_relations_with_grouped_byte_counts() {
    let owned = vec![OwnedRelation {
        name: "orders".to_owned(),
        kind: RelationKind::Table,
        logical_names: BTreeSet::from(["orders".to_owned()]),
        pipeline_names: BTreeSet::new(),
        ownership: BTreeSet::from([Ownership::CurrentManifest]),
    }];
    let catalog = [CatalogRelation {
        name: "orders".to_owned(),
        engine: "MergeTree".to_owned(),
        ownership_generation: Some("g1".to_owned()),
        dependency_names: vec!["orders".to_owned(), "elsewhere".to_owned()],
    }];
    let stats = HashMap::from([("orders".to_owned(), (1_234_567, 3))]);
    let evidence = build_evidence("analytics", &owned, &catalog, &stats, None).unwrap();
    assert_eq!(evidence[0].total_bytes, Some(1_234_567));
    assert_eq!(evidence[0].dependency_relation_names, ["orders"]);
    assert_eq!(
        build_evidence("analytics", &owned, &catalog, &stats, Some(1000)),
        Err(DestructionError::Resource(
            "Warehouse relation DROP limit is 1,000 bytes; oversized resources block destruction \
             before mutation: analytics.orders (1,234,567 bytes)"
                .to_owned()
        ))
    );
    assert_eq!(with_thousands(-1_000), "-1,000");
    assert_eq!(with_thousands(999), "999");
    assert_eq!(
        with_thousands(i128::from(u64::MAX)),
        "18,446,744,073,709,551,615"
    );

    let identity = |relations: &[Evidence]| {
        plan_fingerprint(&PlanIdentity {
            operation: Operation::DestroyPipelines,
            target: "dev",
            database: "analytics",
            metadata_database: "meta",
            requested_pipeline_names: &[],
            included_dependent_pipeline_names: &[],
            affected_pipeline_names: &[],
            affected_model_names: &[],
            affected_source_names: &[],
            relations,
            challenges: &[],
            manifest_fingerprint: "m",
            relation_drop_size_limit: None,
            relation_drop_size_server_limit: None,
            relation_drop_size_override: None,
            relation_drop_size_policy_observed: true,
            include_orphans: false,
        })
    };
    let resized = build_evidence("analytics", &owned, &catalog, &HashMap::new(), None).unwrap();
    assert_eq!(identity(&evidence), identity(&resized));
    assert_ne!(identity(&evidence), identity(&[]));
}

#[test]
fn names_first_middle_and_last_pipelines_as_challenges() {
    assert_eq!(
        build_challenges(&names(&["e", "a", "c", "b", "d", "a"]), true),
        Ok(names(&["a", "c", "e", "PRODUCTION"]))
    );
    assert_eq!(
        build_challenges(&names(&["b", "a"]), false),
        Ok(names(&["a", "b"]))
    );
    assert!(build_challenges(&[], false).is_err());
}

#[test]
fn drop_statements_quote_names_and_apply_the_size_override() {
    let relations = vec![
        (RelationKind::View, "db".to_owned(), "v`1".to_owned()),
        (
            RelationKind::MaterializedView,
            "db".to_owned(),
            "mv".to_owned(),
        ),
        (
            RelationKind::ManagedSource,
            "d`b".to_owned(),
            "kafka".to_owned(),
        ),
    ];
    let statements = drop_statements(&relations, Some(0));
    assert_eq!(
        statements
            .iter()
            .map(|statement| (
                &*statement.step_id,
                &*statement.display_name,
                &*statement.sql
            ))
            .collect::<Vec<_>>(),
        [
            (
                "destroy_relation_0001",
                "Drop view db.v`1",
                "DROP VIEW IF EXISTS `db`.`v``1` SYNC SETTINGS max_table_size_to_drop = 0;",
            ),
            (
                "destroy_relation_0002",
                "Drop table db.mv",
                "DROP TABLE IF EXISTS `db`.`mv` SYNC SETTINGS max_table_size_to_drop = 0;",
            ),
            (
                "destroy_relation_0003",
                "Drop table d`b.kafka",
                "DROP TABLE IF EXISTS `d``b`.`kafka` SYNC SETTINGS max_table_size_to_drop = 0;",
            ),
        ]
    );
    assert_eq!(
        drop_statements(&relations[..1], None)[0].sql,
        "DROP VIEW IF EXISTS `db`.`v``1` SYNC;"
    );
}
