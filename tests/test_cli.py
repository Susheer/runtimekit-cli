import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtimekit_cli.cli import Workspace, collect_release_artifacts, main


def write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class RuntimeKitCliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.old_cli_home = os.environ.get("RUNTIMEKIT_HOME")
        os.environ["RUNTIMEKIT_HOME"] = str(self.root / ".runtimekit-test-home")
        self.create_workspace()

    def tearDown(self):
        if self.old_cli_home is None:
            os.environ.pop("RUNTIMEKIT_HOME", None)
        else:
            os.environ["RUNTIMEKIT_HOME"] = self.old_cli_home
        self.tempdir.cleanup()

    def run_cli(self, args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def create_workspace(self):
        write_json(
            self.root / "spec.json",
            {
                "spec_name": "test_spec",
                "platform": {
                    "prefix": "DISC",
                    "localFolder": "apps/Platform",
                    "runtimeFolder": "apps/Platform/runtime",
                },
                "app": [
                    {
                        "prefix": "DISC",
                        "local": "Platform",
                        "localFolder": "apps/Platform",
                    },
                    {
                        "prefix": "DLMS",
                        "local": "DLMS",
                        "localFolder": "apps/DLMS",
                    },
                    {
                        "prefix": "OMS",
                        "local": "OMS",
                        "localFolder": "apps/OMS",
                    },
                ],
                "build_order": ["DLMS", "OMS", "DISC"],
            },
        )
        (self.root / "local.properties").write_text("root.app=DLMS\n", encoding="utf-8")
        (self.root / "apps/Platform/runtime/staging").mkdir(parents=True, exist_ok=True)
        self.create_module("DLMS", "DLMS.Inventory", "inventory", page_id="Inventory")
        self.create_module("OMS", "OMS.ReceiptModule", "receiptmodule", page_id="receipt_list")
        write_json(
            self.root / "apps/DLMS/Platform/modules/module-descriptor.json",
            {
                "app": "DLMS",
                "ownership": "external",
                "modules": [
                    {
                        "name": "DLMS.Inventory",
                        "directory": "DLMS.Inventory",
                        "publish": True,
                        "enabled": True,
                    }
                ],
            },
        )
        write_json(
            self.root / "apps/OMS/Platform/modules/module-descriptor.json",
            {
                "app": "OMS",
                "ownership": "external",
                "modules": [
                    {
                        "name": "OMS.ReceiptModule",
                        "directory": "OMS.ReceiptModule",
                        "publish": True,
                        "enabled": True,
                    }
                ],
            },
        )
        write_json(
            self.root / "apps/Platform/runtime/staging/runtime-manifest.json",
            {
                "currentSpec": "DLMS",
                "currentSpecSource": "local.properties:root.app",
                "activeSources": ["DLMS", "core"],
                "modules": [
                    {"name": "DLMS.Inventory"},
                    {"name": "OMS.ReceiptModule"},
                ],
                "activeModules": [{"name": "DLMS.Inventory"}],
                "blockedModules": [
                    {"name": "OMS.ReceiptModule", "reason": "source not in active scope"}
                ],
                "includedModules": [],
                "dependencyIncludedModules": [],
                "effectiveModules": {"inventory": {"winner": "DLMS.Inventory"}},
                "pages": [{"id": "Inventory"}],
                "navigation": [],
                "backendLoadOrder": ["DLMS.Inventory"],
            },
        )

    def create_module(self, app, module_name, key, page_id):
        module_root = self.root / "apps" / app / "Platform" / "modules" / module_name
        write_json(
            module_root / "module.json",
            {
                "name": module_name,
                "moduleKey": key,
                "version": "1.0.0",
                "enabled": True,
                "dependencies": [],
                "pages": [
                    {
                        "id": page_id,
                        "pageType": page_id,
                        "name": page_id,
                        "title": page_id,
                        "component": page_id + "Page.jsx",
                        "menu": {"label": page_id, "parent": "Operations"},
                        "multiInstance": True,
                        "order": 0,
                    }
                ],
                "backend": {"routes": [], "services": []},
                "frontend": {"entry": "./frontend/module.js", "stores": [], "slots": []},
            },
        )
        (module_root / "frontend/pages").mkdir(parents=True, exist_ok=True)
        (module_root / "frontend/module.js").write_text(
            "module.exports = { pages: [] };\n", encoding="utf-8"
        )

    def test_add_module_generates_full_stack_module_and_descriptor_entry(self):
        code, stdout, stderr = self.run_cli(
            [
                "add",
                "module",
                "DLMS.WorkOrders",
                "--workspace",
                str(self.root),
                "--backend",
                "--store",
                "--menu-parent",
                "Operations",
                "--order",
                "40",
            ]
        )

        self.assertEqual(code, 0, stderr)
        self.assertIn("create", stdout)
        module_root = self.root / "apps/DLMS/Platform/modules/DLMS.WorkOrders"
        self.assertTrue((module_root / "module.json").exists())
        self.assertTrue((module_root / "backend/routes.js").exists())
        self.assertTrue((module_root / "backend/service.js").exists())
        self.assertTrue((module_root / "frontend/module.js").exists())
        self.assertTrue((module_root / "frontend/pages/WorkOrdersPage.jsx").exists())
        self.assertTrue((module_root / "frontend/stores/WorkOrdersStore.js").exists())

        manifest = read_json(module_root / "module.json")
        self.assertEqual(manifest["name"], "DLMS.WorkOrders")
        self.assertEqual(manifest["moduleKey"], "workorders")
        self.assertEqual(manifest["pages"][0]["id"], "WorkOrders")
        self.assertEqual(manifest["backend"]["routes"][0]["path"], "/api/work-orders")
        self.assertEqual(manifest["frontend"]["stores"][0]["name"], "WorkOrdersStore")

        descriptor = read_json(self.root / "apps/DLMS/Platform/modules/module-descriptor.json")
        names = [entry["name"] for entry in descriptor["modules"]]
        self.assertIn("DLMS.WorkOrders", names)

    def test_dry_run_does_not_write_files(self):
        code, stdout, stderr = self.run_cli(
            [
                "add",
                "module",
                "DLMS.DryRunModule",
                "--workspace",
                str(self.root),
                "--dry-run",
            ]
        )

        self.assertEqual(code, 0, stderr)
        self.assertIn("Would create", stdout)
        self.assertFalse(
            (self.root / "apps/DLMS/Platform/modules/DLMS.DryRunModule").exists()
        )

    def test_add_page_store_backend_include_and_dependency(self):
        self.run_cli(["add", "module", "DLMS.WorkOrders", "--workspace", str(self.root)])

        code, _, stderr = self.run_cli(
            [
                "add",
                "page",
                "DLMS.WorkOrders",
                "WorkOrderDetails",
                "--workspace",
                str(self.root),
                "--order",
                "50",
            ]
        )
        self.assertEqual(code, 0, stderr)

        code, _, stderr = self.run_cli(
            [
                "add",
                "store",
                "DLMS.WorkOrders",
                "WorkOrdersDetailStore",
                "--workspace",
                str(self.root),
                "--page",
                "WorkOrderDetails",
            ]
        )
        self.assertEqual(code, 0, stderr)

        code, _, stderr = self.run_cli(
            ["add", "backend", "DLMS.WorkOrders", "--workspace", str(self.root)]
        )
        self.assertEqual(code, 0, stderr)

        code, _, stderr = self.run_cli(
            [
                "add",
                "include",
                "OMS.ReceiptModule",
                "--app",
                "DLMS",
                "--workspace",
                str(self.root),
                "--reason",
                "Expose receipts",
            ]
        )
        self.assertEqual(code, 0, stderr)

        code, _, stderr = self.run_cli(
            [
                "add",
                "dependency",
                "DLMS.WorkOrders",
                "OMS.ReceiptModule",
                "--workspace",
                str(self.root),
            ]
        )
        self.assertEqual(code, 0, stderr)

        manifest = read_json(
            self.root / "apps/DLMS/Platform/modules/DLMS.WorkOrders/module.json"
        )
        page_ids = [page["id"] for page in manifest["pages"]]
        self.assertIn("WorkOrderDetails", page_ids)
        self.assertIn("OMS.ReceiptModule", manifest["dependencies"])
        self.assertEqual(manifest["backend"]["routes"][0]["path"], "/api/work-orders")
        store_names = [store["name"] for store in manifest["frontend"]["stores"]]
        self.assertIn("WorkOrdersDetailStore", store_names)

        descriptor = read_json(self.root / "apps/DLMS/Platform/modules/module-descriptor.json")
        self.assertEqual(descriptor["includes"][0]["module"], "OMS.ReceiptModule")

    def test_inspect_and_list_commands_read_workspace_state(self):
        code, stdout, stderr = self.run_cli(
            ["inspect", "runtime", "--workspace", str(self.root), "--json"]
        )
        self.assertEqual(code, 0, stderr)
        runtime = json.loads(stdout)
        self.assertEqual(runtime["currentSpec"], "DLMS")
        self.assertIn("DLMS.Inventory", runtime["activeModules"])
        self.assertEqual(runtime["blockedModules"][0]["name"], "OMS.ReceiptModule")

        code, stdout, stderr = self.run_cli(
            ["list", "apps", "--workspace", str(self.root), "--json"]
        )
        self.assertEqual(code, 0, stderr)
        apps = json.loads(stdout)
        self.assertEqual(apps["rootApp"], "DLMS")

        code, stdout, stderr = self.run_cli(
            ["list", "modules", "--workspace", str(self.root), "--json"]
        )
        self.assertEqual(code, 0, stderr)
        modules = json.loads(stdout)["modules"]
        self.assertIn("DLMS.Inventory", [module["name"] for module in modules])

    def test_prepare_validate_and_test_support_dry_run(self):
        for args, expected in (
            (["prepare"], "npm run prepare:runtime"),
            (["validate"], "npm run validate:precedence"),
            (["test"], "npm run test"),
        ):
            code, stdout, stderr = self.run_cli(
                args + ["--workspace", str(self.root), "--dry-run"]
            )
            self.assertEqual(code, 0, stderr)
            self.assertIn(expected, stdout)

    def test_run_commands_support_dry_run(self):
        cases = (
            (["run", "browser"], "npm run dev"),
            (["run", "browser", "--server-only"], "npm run server"),
            (["run", "browser", "--production"], "npm run start"),
            (["run", "electron"], "npm run electron"),
        )
        for args, expected in cases:
            code, stdout, stderr = self.run_cli(
                args + ["--workspace", str(self.root), "--dry-run"]
            )
            self.assertEqual(code, 0, stderr)
            self.assertIn(expected, stdout)

        code, stdout, stderr = self.run_cli(
            [
                "run",
                "browser",
                "--workspace",
                str(self.root),
                "--dry-run",
                "--open",
                "--open-delay",
                "0.1",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("Open browser: http://localhost:4100 after 0.1s", stdout)
        self.assertIn("npm run dev", stdout)

    def test_release_commands_support_dry_run(self):
        code, stdout, stderr = self.run_cli(
            [
                "release",
                "prepare",
                "--workspace",
                str(self.root),
                "--dry-run",
                "--target",
                "mac-dmg",
                "--config",
                "apps/DLMS/Platform/release/release.json",
                "--output",
                "apps/DLMS/Platform/release/out",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("npm run release:prepare --", stdout)
        self.assertIn("--target=mac-dmg", stdout)
        self.assertIn("--config=apps/DLMS/Platform/release/release.json", stdout)
        self.assertIn("--output=apps/DLMS/Platform/release/out", stdout)

        code, stdout, stderr = self.run_cli(
            [
                "release",
                "make",
                "--workspace",
                str(self.root),
                "--dry-run",
                "--target",
                "windows-msi",
                "--target",
                "windows-exe",
                "--arch",
                "x64",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("npm run release:make --", stdout)
        self.assertIn("--target=windows-msi", stdout)
        self.assertIn("--target=windows-exe", stdout)
        self.assertIn("--arch=x64", stdout)
        self.assertIn("Collect release artifacts:", stdout)

    def test_config_init_and_show(self):
        code, stdout, stderr = self.run_cli(
            [
                "config",
                "init",
                "--workspace",
                str(self.root),
                "--installer-dir",
                "published-installers",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("Configured runtime workspace", stdout)

        code, stdout, stderr = self.run_cli(["config", "show", "--json"])
        self.assertEqual(code, 0, stderr)
        config = json.loads(stdout)
        self.assertEqual(config["defaultWorkspace"], str(self.root.resolve()))
        self.assertEqual(config["installerDir"], "published-installers")

    def test_collect_release_artifacts_copies_to_root_installer_folder(self):
        artifact = self.root / "apps/Platform/release/out/make/darwin/arm64/Test.dmg"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("installer", encoding="utf-8")

        workspace = Workspace.discover(str(self.root))
        collect_release_artifacts(
            workspace,
            "apps/Platform/release/out",
            "installer",
            dry_run=False,
        )

        copied = self.root / "installer/darwin/arm64/Test.dmg"
        self.assertTrue(copied.exists())
        self.assertEqual(copied.read_text(encoding="utf-8"), "installer")

    def test_remove_module_deletes_files_descriptor_and_references(self):
        self.run_cli(["add", "module", "DLMS.WorkOrders", "--workspace", str(self.root)])
        self.run_cli(
            [
                "add",
                "dependency",
                "DLMS.Inventory",
                "DLMS.WorkOrders",
                "--workspace",
                str(self.root),
            ]
        )
        self.run_cli(
            [
                "add",
                "include",
                "DLMS.WorkOrders",
                "--app",
                "DLMS",
                "--workspace",
                str(self.root),
            ]
        )

        code, stdout, stderr = self.run_cli(
            [
                "remove",
                "module",
                "DLMS.WorkOrders",
                "--workspace",
                str(self.root),
                "--yes",
            ]
        )

        self.assertEqual(code, 0, stderr)
        self.assertIn("remove", stdout)
        self.assertFalse((self.root / "apps/DLMS/Platform/modules/DLMS.WorkOrders").exists())

        descriptor = read_json(self.root / "apps/DLMS/Platform/modules/module-descriptor.json")
        self.assertNotIn("DLMS.WorkOrders", [entry["name"] for entry in descriptor["modules"]])
        self.assertEqual(descriptor.get("includes"), [])

        inventory = read_json(self.root / "apps/DLMS/Platform/modules/DLMS.Inventory/module.json")
        self.assertNotIn("DLMS.WorkOrders", inventory["dependencies"])

    def test_remove_module_requires_yes_unless_dry_run(self):
        self.run_cli(["add", "module", "DLMS.WorkOrders", "--workspace", str(self.root)])

        code, _, stderr = self.run_cli(
            ["remove", "module", "DLMS.WorkOrders", "--workspace", str(self.root)]
        )
        self.assertEqual(code, 1)
        self.assertIn("destructive", stderr)

        code, stdout, stderr = self.run_cli(
            [
                "remove",
                "module",
                "DLMS.WorkOrders",
                "--workspace",
                str(self.root),
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("Would remove", stdout)
        self.assertTrue((self.root / "apps/DLMS/Platform/modules/DLMS.WorkOrders").exists())

    def test_clean_removes_selected_generated_outputs(self):
        build_bin = self.root / "apps/Platform/runtime/build-bin"
        installer = self.root / "installer"
        staging = self.root / "apps/Platform/runtime/staging"
        release_out = self.root / "apps/Platform/release/out"
        for path in (build_bin, installer, staging, release_out):
            path.mkdir(parents=True, exist_ok=True)
            (path / "marker.txt").write_text("x", encoding="utf-8")

        code, stdout, stderr = self.run_cli(
            [
                "clean",
                "--workspace",
                str(self.root),
                "--target",
                "build",
                "--target",
                "installer",
                "--yes",
            ]
        )

        self.assertEqual(code, 0, stderr)
        self.assertIn("remove", stdout)
        self.assertFalse(build_bin.exists())
        self.assertFalse(installer.exists())
        self.assertTrue(staging.exists())
        self.assertTrue(release_out.exists())

    def test_clean_requires_yes_unless_dry_run(self):
        code, _, stderr = self.run_cli(
            ["clean", "--workspace", str(self.root), "--target", "build"]
        )
        self.assertEqual(code, 1)
        self.assertIn("destructive", stderr)

        code, stdout, stderr = self.run_cli(
            ["clean", "--workspace", str(self.root), "--target", "build", "--dry-run"]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("Would remove", stdout)


if __name__ == "__main__":
    unittest.main()
