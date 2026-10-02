from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "orchestrator/storage/migrations/versions/20261002_0134_remove_injected_maven_configuration.py"
)


def load_migration():
    spec = importlib.util.spec_from_file_location("maven_cleanup_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_configs():
    original = {
        "services": {
            "api": {
                "environment": {
                    "APP_MODE": "explicit",
                    "SPRING_PROFILES_ACTIVE": "custom",
                },
                "depends_on": ["database"],
            },
            "database": {
                "image": "postgres:16",
                "environment": {"POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:?required}"},
            },
        }
    }
    generated = {
        "services": {
            "api": {
                "build": {
                    "context": ".",
                    "dockerfile_inline": 'FROM maven:3.9.9-eclipse-temurin-17 AS build\nRUN mkdir -p /tmp/master-builder-config && echo generated\nFROM eclipse-temurin:17-jre\nCOPY --from=build /tmp/app.jar /app/app.jar\nCOPY --from=build /tmp/master-builder-config /app/master-builder-config\nRUN cat > /app/master-builder-logback.xml <<\'XML\'\n<configuration/>\nXML\nENTRYPOINT ["java","-jar","/app/app.jar"]\n',
                },
                "environment": {
                    "APP_MODE": "explicit",
                    "SPRING_PROFILES_ACTIVE": "generated-profile",
                    "SPRING_CONFIG_LOCATION": "file:/app/master-builder-config/",
                    "GENERATED_ONLY": "injected",
                },
                "depends_on": {"database": {"condition": "service_healthy"}},
            },
            "database": {
                "build": {
                    "context": ".",
                    "dockerfile_inline": "FROM postgres:16\nRUN cat > /docker-entrypoint-initdb.d/20-master-builder-app-roles.sh <<'SCRIPT'\nframework-generated\nSCRIPT\n",
                },
                "environment": original["services"]["database"]["environment"],
            },
        }
    }
    compose = yaml.safe_dump(generated)
    config = {
        "source_strategy": "docker_compose",
        "generated_compose_raw": compose,
        "deployment_plan": {
            "services": [
                {"key": "api", "compose_service": "api", "build_strategy": "maven"}
            ],
            "compose_raw": compose,
        },
    }
    return original, config


def test_migration_restores_original_values_and_removes_only_proven_generated_fields():
    migration = load_migration()
    original, config = fixture_configs()
    evidence = [(config, original)]
    updated, changed = migration.clean_configuration(config, evidence)
    assert changed
    compose = yaml.safe_load(updated["generated_compose_raw"])
    assert (
        compose["services"]["api"]["environment"]
        == original["services"]["api"]["environment"]
    )
    assert compose["services"]["api"]["depends_on"] == ["database"]
    assert compose["services"]["database"] == original["services"]["database"]
    assert (
        "master-builder-config"
        not in compose["services"]["api"]["build"]["dockerfile_inline"]
    )
    assert (
        "master-builder-logback"
        not in compose["services"]["api"]["build"]["dockerfile_inline"]
    )
    assert updated["deployment_plan"]["compose_raw"] == updated["generated_compose_raw"]
    second, changed = migration.clean_configuration(updated, evidence)
    assert not changed and second == updated


def test_migration_preserves_later_explicit_edits():
    migration = load_migration()
    original, baseline = fixture_configs()
    compose = yaml.safe_load(baseline["generated_compose_raw"])
    compose["services"]["api"]["environment"]["GENERATED_ONLY"] = "operator-edit"
    compose["services"]["api"]["environment"]["NEW_EXPLICIT"] = "kept"
    current = {
        **baseline,
        "generated_compose_raw": yaml.safe_dump(compose),
        "deployment_plan": {
            **baseline["deployment_plan"],
            "compose_raw": yaml.safe_dump(compose),
        },
    }
    updated, changed = migration.clean_configuration(current, [(baseline, original)])
    assert changed
    environment = yaml.safe_load(updated["generated_compose_raw"])["services"]["api"][
        "environment"
    ]
    assert environment == {
        **original["services"]["api"]["environment"],
        "GENERATED_ONLY": "operator-edit",
        "NEW_EXPLICIT": "kept",
    }


def test_migration_rejects_missing_or_conflicting_provenance_without_values_in_error():
    migration = load_migration()
    original, config = fixture_configs()
    different_original = {"services": {"api": {"environment": {"APP_MODE": "another"}}}}
    for evidence in ([], [(config, original), (config, different_original)]):
        with pytest.raises(
            RuntimeError, match="unambiguous original planner evidence"
        ) as error:
            migration.clean_configuration(config, evidence)
        assert "generated-profile" not in str(error.value)
        assert "another" not in str(error.value)


def test_migration_preserves_generic_resource_alias_normalization():
    migration = load_migration()
    original, config = fixture_configs()
    original["services"]["api"]["environment"]["APP_DATABASE_URL"] = (
        "jdbc:postgresql://database:5432/example"
    )
    generated = yaml.safe_load(config["generated_compose_raw"])
    generated["services"]["api"]["environment"]["APP_DATABASE_URL"] = (
        "jdbc:postgresql://mb-database:5432/example"
    )
    compose_raw = yaml.safe_dump(generated)
    config = {
        **config,
        "generated_compose_raw": compose_raw,
        "deployment_plan": {**config["deployment_plan"], "compose_raw": compose_raw},
    }
    updated, changed = migration.clean_configuration(config, [(config, original)])
    assert changed
    environment = yaml.safe_load(updated["generated_compose_raw"])["services"]["api"][
        "environment"
    ]
    assert (
        environment["APP_DATABASE_URL"] == "jdbc:postgresql://mb-database:5432/example"
    )


def test_sqlite_upgrade_cleans_app_project_release_and_analysis_snapshots():
    import sqlalchemy as sa
    from unittest.mock import patch

    migration = load_migration()
    original, config = fixture_configs()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    tables = {}
    for table_name, identifier, column in (
        ("projects", "project_id", "deployment_config"),
        ("project_apps", "app_id", "deployment_config"),
        ("project_deployment_releases", "release_id", "deployment_snapshot"),
        ("project_app_analysis_runs", "run_id", "result_payload"),
    ):
        columns = [
            sa.Column(identifier, sa.String(), primary_key=True),
            sa.Column("tenant_id", sa.String()),
        ]
        if identifier != "project_id":
            columns.append(sa.Column("project_id", sa.String()))
        columns.append(sa.Column(column, sa.JSON()))
        tables[table_name] = sa.Table(table_name, metadata, *columns)
    metadata.create_all(engine)
    with engine.begin() as connection:
        for table_name, table in tables.items():
            identifier = list(table.primary_key.columns)[0].name
            column = list(table.columns)[-1].name
            payload = config
            if table_name == "project_app_analysis_runs":
                payload = {
                    "raw_planner_result_json": {
                        "deployment": {"compose_raw": yaml.safe_dump(original)}
                    },
                    "normalized_apps": [{"deployment_config": config}],
                }
            elif table_name == "project_deployment_releases":
                payload = {"deployment_config": config}
            connection.execute(
                table.insert().values(
                    {
                        identifier: "project-1"
                        if identifier == "project_id"
                        else "record-1",
                        "tenant_id": "tenant-1",
                        "project_id": "project-1",
                        column: payload,
                    }
                )
            )
        with patch.object(migration.op, "get_bind", return_value=connection):
            migration.upgrade()
            migration.upgrade()
        for table_name, table in tables.items():
            payload = connection.execute(
                sa.select(list(table.columns)[-1])
            ).scalar_one()
            if table_name == "project_app_analysis_runs":
                payload = payload["normalized_apps"][0]["deployment_config"]
            elif table_name == "project_deployment_releases":
                payload = payload["deployment_config"]
            compose = yaml.safe_load(payload["generated_compose_raw"])
            assert (
                compose["services"]["api"]["environment"]
                == original["services"]["api"]["environment"]
            )
            assert (
                "master-builder-config"
                not in compose["services"]["api"]["build"]["dockerfile_inline"]
            )


def test_migration_cleans_each_paired_compose_field_independently():
    import copy

    migration = load_migration()
    original, baseline = fixture_configs()
    cleaned, _ = migration.clean_configuration(baseline, [(baseline, original)])
    for field in ("generated_compose_raw", "deployment_plan"):
        partial = copy.deepcopy(cleaned)
        partial[field] = copy.deepcopy(baseline[field])
        updated, changed = migration.clean_configuration(
            partial, [(baseline, original)]
        )
        assert changed
        assert updated == cleaned


def test_migration_rejects_original_user_owned_generated_artifacts():
    import copy

    migration = load_migration()
    original, baseline = fixture_configs()
    original["services"]["api"]["build"] = copy.deepcopy(
        yaml.safe_load(baseline["generated_compose_raw"])["services"]["api"]["build"]
    )
    with pytest.raises(RuntimeError, match="unambiguous original planner evidence"):
        migration.clean_configuration(baseline, [(baseline, original)])


@pytest.mark.parametrize("normalized_apps", [[], None, "unrelated"])
def test_upgrade_ignores_unrelated_malformed_original_evidence(normalized_apps):
    from unittest.mock import patch
    import sqlalchemy as sa

    migration = load_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    analysis = sa.Table(
        "project_app_analysis_runs",
        metadata,
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String()),
        sa.Column("project_id", sa.String()),
        sa.Column("result_payload", sa.JSON()),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            analysis.insert().values(
                run_id="unrelated",
                tenant_id="tenant-1",
                project_id="project-1",
                result_payload={
                    "raw_planner_result_json": {
                        "deployment": {"compose_raw": "services: ["}
                    },
                    "normalized_apps": normalized_apps,
                },
            )
        )
        with patch.object(migration.op, "get_bind", return_value=connection):
            migration.upgrade()


def test_migration_rejects_relevant_malformed_original_evidence():
    migration = load_migration()
    _, baseline = fixture_configs()
    with pytest.raises(RuntimeError, match="unambiguous original planner evidence"):
        migration.clean_configuration(baseline, [(baseline, "services: [")])


def test_migration_preserves_valid_yaml_date_extensions():
    import datetime

    migration = load_migration()
    original, baseline = fixture_configs()
    original["x-release-date"] = datetime.date(2026, 10, 2)
    generated = yaml.safe_load(baseline["generated_compose_raw"])
    generated["x-release-date"] = original["x-release-date"]
    raw = yaml.safe_dump(generated)
    baseline["generated_compose_raw"] = raw
    baseline["deployment_plan"]["compose_raw"] = raw
    updated, changed = migration.clean_configuration(baseline, [(baseline, original)])
    assert changed
    assert (
        yaml.safe_load(updated["generated_compose_raw"])["x-release-date"]
        == original["x-release-date"]
    )


def test_migration_rejects_later_resource_build_edits_retaining_private_artifacts():
    import copy

    migration = load_migration()
    original, baseline = fixture_configs()
    current = copy.deepcopy(baseline)
    compose = yaml.safe_load(current["generated_compose_raw"])
    compose["services"]["database"]["build"]["args"] = {"EXPLICIT": "kept"}
    current["generated_compose_raw"] = yaml.safe_dump(compose)
    with pytest.raises(RuntimeError, match="unambiguous original planner evidence"):
        migration.clean_configuration(current, [(baseline, original)])
