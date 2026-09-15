import unittest
from unittest import mock

from court import migration_guard


MERGE_FILE_TEXT = """from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [
        ('common', '0130_marketplacealias_shopify_available_quantity_and_more'),
        ('common', '0130_marketplacealias_vendor'),
    ]
    operations = []
"""

CUSTOM_NAMED_MERGE_TEXT = """from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [
        ('marketing', '0076_directmailjob_undeliverable_reason_and_more'),
        ('marketing', '0076_campaign_shortlink_verification_run'),
    ]
    operations = [
    ]
"""

SCHEMA_MIGRATION_TEXT = """from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ('common', '0129_merge_20260912_0825'),
    ]
    operations = [
        migrations.AddField('marketplacealias', 'vendor', models.CharField(default='')),
    ]
"""

DATA_MIGRATION_TEXT = """from django.db import migrations

def forwards(apps, schema_editor):
    pass

class Migration(migrations.Migration):
    dependencies = [
        ('common', '0130_vendor'),
    ]
    operations = [
        migrations.RunPython(forwards),
    ]
"""


class TestPathClassification(unittest.TestCase):
    def test_migration_app(self):
        self.assertEqual(migration_guard.migration_app("apps/common/migrations/0130_vendor.py"), "common")
        self.assertEqual(migration_guard.migration_app("apps/crm/migrations/0054_x.py"), "crm")

    def test_migration_app_rejects_non_migration_paths(self):
        self.assertIsNone(migration_guard.migration_app("apps/common/views.py"))
        self.assertIsNone(migration_guard.migration_app("core/migrations/0001_initial.py"))
        self.assertIsNone(migration_guard.migration_app("manage.py"))

    def test_is_migration_file_excludes_init_and_scratch(self):
        self.assertTrue(migration_guard.is_migration_file("apps/common/migrations/0130_vendor.py"))
        self.assertFalse(migration_guard.is_migration_file("apps/common/migrations/__init__.py"))
        self.assertFalse(migration_guard.is_migration_file("apps/common/migrations/notes.py"))

    def test_is_merge_migration_filename(self):
        self.assertTrue(migration_guard.is_merge_migration_filename("apps/common/migrations/0131_merge_20260914_1200.py"))
        self.assertTrue(migration_guard.is_merge_migration_filename("apps/common/migrations/0129_merge_20260912_0825.py"))
        self.assertFalse(migration_guard.is_merge_migration_filename("apps/common/migrations/0130_vendor.py"))
        self.assertFalse(migration_guard.is_merge_migration_filename("apps/common/migrations/0130_add_merge_fields.py"))


class TestMergeShapedContent(unittest.TestCase):
    def test_standard_merge_node(self):
        self.assertTrue(migration_guard.is_merge_shaped_content(MERGE_FILE_TEXT, "common"))

    def test_custom_named_merge_node(self):
        self.assertTrue(migration_guard.is_merge_shaped_content(CUSTOM_NAMED_MERGE_TEXT, "marketing"))

    def test_schema_migration_is_not_merge(self):
        self.assertFalse(migration_guard.is_merge_shaped_content(SCHEMA_MIGRATION_TEXT, "common"))

    def test_data_migration_is_not_merge(self):
        self.assertFalse(migration_guard.is_merge_shaped_content(DATA_MIGRATION_TEXT, "common"))

    def test_zero_ops_cross_app_deps_is_not_merge(self):
        text = MERGE_FILE_TEXT.replace("('common', '0130_marketplacealias_vendor')", "('crm', '0054_x')")
        self.assertFalse(migration_guard.is_merge_shaped_content(text, "common"))


class TestScanBranchContraband(unittest.TestCase):
    def _patch_git(self, added_paths, blobs=None):
        blobs = blobs or {}
        return (
            mock.patch.object(migration_guard, "_added_migration_paths", return_value=added_paths),
            mock.patch.object(migration_guard, "_read_blob", side_effect=lambda root, ref, path: blobs.get(path, "")),
        )

    def test_flags_filename_merge_and_custom_named_merge(self):
        added = [
            "apps/common/migrations/0131_merge_20260914_1200.py",
            "apps/marketing/migrations/0077_cogship081_marketing_leaf_merge.py",
        ]
        blobs = {"apps/marketing/migrations/0077_cogship081_marketing_leaf_merge.py": CUSTOM_NAMED_MERGE_TEXT}
        p1, p2 = self._patch_git(added, blobs)
        with p1, p2:
            hits = migration_guard.scan_branch_contraband(root="/repo", base_ref="castle", branch_ref="quest/x")
        self.assertEqual([h.path for h in hits], added)
        self.assertEqual([h.reason for h in hits], ["filename-merge", "zero-op-merge"])
        self.assertEqual(hits[0].app, "common")

    def test_ignores_schema_and_data_migrations(self):
        added = [
            "apps/common/migrations/0130_marketplacealias_vendor.py",
            "apps/common/migrations/0131_backfill.py",
        ]
        blobs = {"apps/common/migrations/0131_backfill.py": DATA_MIGRATION_TEXT}
        p1, p2 = self._patch_git(added, blobs)
        with p1, p2:
            hits = migration_guard.scan_branch_contraband(root="/repo", base_ref="castle", branch_ref="quest/x")
        self.assertEqual(hits, [])

    def test_remediation_message_names_files_and_procedure(self):
        hit = migration_guard.ContrabandHit(
            path="apps/common/migrations/0131_merge_20260914_1200.py", app="common", reason="filename-merge"
        )
        msg = migration_guard.remediation_message([hit], base_ref="castle")
        self.assertIn("0131_merge_20260914_1200.py", msg)
        self.assertIn("git rm", msg)
        self.assertIn("git merge castle", msg)
        self.assertIn("makemigrations", msg)


class TestMigrationActivity(unittest.TestCase):
    def test_groups_added_migrations_by_app_with_quest_ids(self):
        added_by_branch = {
            ("Q391", "quest/q391-x"): ["apps/common/migrations/0130_a.py", "apps/common/views.py"],
            ("Q401", "quest/q401-y"): ["apps/common/migrations/0130_b.py", "apps/crm/migrations/0054_z.py"],
        }

        def fake_added(root, base_ref, branch_ref):
            for (qid, ref), paths in added_by_branch.items():
                if ref == branch_ref:
                    return paths
            return []

        with mock.patch.object(migration_guard, "_added_migration_paths", side_effect=fake_added):
            activity = migration_guard.migration_activity(
                root="/repo",
                branches=[("Q391", "quest/q391-x"), ("Q401", "quest/q401-y")],
                base_ref="castle",
            )
        self.assertEqual(
            {e["quest_id"] for e in activity["common"]}, {"Q391", "Q401"}
        )
        self.assertEqual({e["quest_id"] for e in activity["crm"]}, {"Q401"})
        self.assertNotIn("views.py", str(activity))


if __name__ == "__main__":
    unittest.main()
