import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


class CliError(Exception):
    pass


def normalize_app_name(value):
    if not value:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    upper = normalized.upper()
    if upper in ("DISC", "PLATFORM", "RUNTIME", "CORE"):
        return "core"
    return upper


def split_words(value):
    text = re.sub(r"[_\-.]+", " ", str(value or ""))
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [word for word in text.strip().split() if word]


def pascal_case(value):
    words = split_words(value)
    return "".join(word[:1].upper() + word[1:] for word in words) or "Module"


def title_case(value):
    words = split_words(value)
    return " ".join(word[:1].upper() + word[1:] for word in words) or "Module"


def kebab_case(value):
    words = split_words(value)
    return "-".join(word.lower() for word in words) or "module"


def module_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower()) or "module"


def parse_properties(source):
    result = {}
    for raw_line in str(source or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        match = re.match(r"^([^:=\s]+)\s*(?:[:=]|\s+)\s*(.*)$", line)
        if match:
            result[match.group(1).strip()] = match.group(2).strip()
        else:
            result[line] = ""
    return result


def read_json(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def format_json(payload):
    return json.dumps(payload, indent=2) + "\n"


def parse_module_name(name):
    parts = str(name or "").split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise CliError("Module names must be app-qualified, for example DLMS.WorkOrders.")
    return parts[0].upper(), parts[1]


def dependency_label(dependency):
    if isinstance(dependency, str):
        return dependency
    if isinstance(dependency, dict):
        return dependency.get("module") or dependency.get("name") or ""
    return ""


def cli_config_path():
    cli_home = os.environ.get("RUNTIMEKIT_HOME")
    if cli_home:
        return Path(cli_home).expanduser().resolve() / "config.json"
    return Path.home() / ".runtimekit" / "config.json"


def load_cli_config():
    path = cli_config_path()
    if not path.exists():
        return {}
    return read_json(path)


def save_cli_config(payload):
    path = cli_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_json(payload), encoding="utf-8")


def clear_cli_config():
    path = cli_config_path()
    if path.exists():
        path.unlink()


def resolve_workspace_path(workspace, candidate):
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if path.is_absolute():
        return path
    return workspace.root / path


class ChangeSet:
    def __init__(self, dry_run=False, force=False):
        self.dry_run = bool(dry_run)
        self.force = bool(force)
        self.changes = []

    def write_text(self, path, content, overwrite=False):
        target = Path(path)
        existed = target.exists()
        if existed:
            current = target.read_text(encoding="utf-8")
            if current == content:
                self.changes.append("unchanged " + str(target))
                return
            if not overwrite and not self.force:
                raise CliError(str(target) + " already exists. Use --force to overwrite it.")

        action = "update" if existed else "create"
        self.changes.append(action + " " + str(target))
        if self.dry_run:
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def write_json(self, path, payload, overwrite=True):
        self.write_text(path, format_json(payload), overwrite=overwrite)

    def print_summary(self):
        if not self.changes:
            print("No changes.")
            return
        prefix = "Would " if self.dry_run else ""
        for change in self.changes:
            print(prefix + change)


def require_yes(args, action):
    if getattr(args, "dry_run", False):
        return
    if not getattr(args, "yes", False):
        raise CliError(action + " is destructive. Re-run with --yes or use --dry-run first.")


def remove_path(path, dry_run=False):
    target = Path(path)
    if not target.exists():
        print(("Would remove " if dry_run else "missing ") + str(target))
        return
    print(("Would remove " if dry_run else "remove ") + str(target))
    if dry_run:
        return
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def copy_file(source, target, dry_run=False):
    print(("Would copy " if dry_run else "copy ") + str(source) + " -> " + str(target))
    if dry_run:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.spec_path = self.root / "spec.json"
        if not self.spec_path.exists():
            raise CliError("Could not find spec.json at " + str(self.spec_path))
        self.spec = read_json(self.spec_path)
        self.local_properties_path = self.root / "local.properties"
        self.local_properties = (
            parse_properties(self.local_properties_path.read_text(encoding="utf-8"))
            if self.local_properties_path.exists()
            else {}
        )

    @classmethod
    def discover(cls, start=None):
        if start:
            current = Path(start).expanduser().resolve()
            if current.is_file():
                current = current.parent
            for candidate in [current] + list(current.parents):
                if (candidate / "spec.json").exists():
                    return cls(candidate)
            raise CliError("Could not discover a runtime workspace from " + str(current) + ".")

        current = Path(os.getcwd()).resolve()
        if current.is_file():
            current = current.parent
        for candidate in [current] + list(current.parents):
            if (candidate / "spec.json").exists():
                return cls(candidate)

        configured_workspace = load_cli_config().get("defaultWorkspace")
        if configured_workspace:
            configured = Path(configured_workspace).expanduser().resolve()
            if (configured / "spec.json").exists():
                return cls(configured)
        raise CliError("Could not discover a runtime workspace. Pass --workspace <path>.")

    def app_definitions(self):
        apps = self.spec.get("app")
        if isinstance(apps, list):
            return apps
        apps = self.spec.get("apps")
        return apps if isinstance(apps, list) else []

    def root_app(self):
        for key in (
            "root.app",
            "root.app.name",
            "rootApp",
            "rootAppName",
            "application.root",
            "application.rootApp",
        ):
            if self.local_properties.get(key):
                return normalize_app_name(self.local_properties[key]), "local.properties:" + key
        return None, None

    def find_app(self, app_name):
        wanted = normalize_app_name(app_name)
        for app_definition in self.app_definitions():
            normalized = normalize_app_name(
                app_definition.get("prefix") or app_definition.get("local")
            )
            if normalized == wanted:
                return app_definition
        if wanted == "core":
            platform = self.spec.get("platform") if isinstance(self.spec.get("platform"), dict) else {}
            return {
                "prefix": "DISC",
                "local": self.framework_folder_name(),
                "localFolder": platform.get("localFolder", "apps/" + self.framework_folder_name()),
            }
        return None

    def runtime_folder(self):
        platform = self.spec.get("platform") if isinstance(self.spec.get("platform"), dict) else {}
        runtime_folder = platform.get("runtimeFolder") or "apps/" + self.framework_folder_name() + "/runtime"
        return self.root / runtime_folder

    def framework_folder_name(self):
        platform = self.spec.get("platform") if isinstance(self.spec.get("platform"), dict) else {}
        local_folder = platform.get("localFolder")
        if local_folder:
            name = Path(local_folder).name
            if name:
                return name
        return "Platform"

    def modules_root(self, app_name):
        normalized = normalize_app_name(app_name)
        app_definition = self.find_app(app_name)
        if not app_definition:
            raise CliError("Unknown app '" + str(app_name) + "'.")
        if normalized == "core":
            return self.runtime_folder() / "modules"
        local_folder = app_definition.get("localFolder")
        if not local_folder:
            raise CliError("App '" + str(app_name) + "' does not define localFolder.")
        return self.root / local_folder / self.framework_folder_name() / "modules"

    def descriptor_path(self, app_name):
        return self.modules_root(app_name) / "module-descriptor.json"

    def load_descriptor(self, app_name):
        path = self.descriptor_path(app_name)
        if path.exists():
            return read_json(path)
        return {
            "app": normalize_app_name(app_name) if normalize_app_name(app_name) != "core" else self.framework_folder_name(),
            "ownership": "core" if normalize_app_name(app_name) == "core" else "external",
            "modules": [],
        }

    def module_root(self, module_name):
        app_name, _ = parse_module_name(module_name)
        descriptor = self.load_descriptor(app_name)
        for entry in descriptor.get("modules", []):
            if isinstance(entry, str):
                entry_name = entry
                directory = entry
            else:
                entry_name = entry.get("name") or entry.get("directory")
                directory = entry.get("directory") or entry.get("folder") or entry_name
            if entry_name == module_name and directory:
                return self.modules_root(app_name) / directory
        return self.modules_root(app_name) / module_name

    def module_manifest_path(self, module_name):
        return self.module_root(module_name) / "module.json"

    def load_module_manifest(self, module_name):
        path = self.module_manifest_path(module_name)
        if not path.exists():
            raise CliError("Module manifest not found: " + str(path))
        return read_json(path)

    def module_exists(self, module_name):
        return self.module_manifest_path(module_name).exists()

    def iter_descriptor_modules(self):
        for app_definition in self.app_definitions():
            app_name = app_definition.get("prefix") or app_definition.get("local")
            if not app_name:
                continue
            descriptor_path = self.descriptor_path(app_name)
            if not descriptor_path.exists():
                continue
            descriptor = read_json(descriptor_path)
            for entry in descriptor.get("modules", []):
                if isinstance(entry, str):
                    name = entry
                    directory = entry
                    enabled = True
                    publish = True
                else:
                    name = entry.get("name") or entry.get("directory")
                    directory = entry.get("directory") or entry.get("folder") or name
                    enabled = entry.get("enabled", True)
                    publish = entry.get("publish", True)
                if name:
                    yield {
                        "app": normalize_app_name(app_name),
                        "name": name,
                        "directory": directory,
                        "enabled": enabled,
                        "publish": publish,
                        "descriptorPath": descriptor_path,
                    }

    def runtime_manifest_path(self, mode="development"):
        runtime = self.runtime_folder()
        if mode == "production":
            return runtime / "build-bin" / "runtime-manifest.json"
        return runtime / "staging" / "runtime-manifest.json"

    def default_release_output_root(self):
        return self.runtime_folder().parent / "release" / "out"

    def installer_root(self, candidate=None):
        configured = load_cli_config().get("installerDir") or "installer"
        return resolve_workspace_path(self, candidate or configured)


def create_module_manifest(module_name, page_id, page_title, options):
    app_name, tail = parse_module_name(module_name)
    key = options.module_key or module_key(tail)
    pages = []
    stores = []

    if not options.no_page:
        pages.append(
            {
                "id": page_id,
                "pageType": page_id,
                "name": page_title,
                "title": page_title,
                "component": page_id + "Page.jsx",
                "menu": {
                    "label": page_title,
                    "parent": options.menu_parent,
                },
                "multiInstance": True,
                "order": options.order,
            }
        )

    if options.store:
        store_name = pascal_case(tail) + "Store"
        stores.append({"name": store_name, "pages": [page_id] if pages else []})

    backend = {"routes": [], "services": []}
    if options.backend:
        service_name = module_key(tail) + "Service"
        backend = {
            "routes": [
                {
                    "path": "/api/" + kebab_case(tail),
                    "methods": ["GET"],
                    "permissions": ["read"],
                }
            ],
            "services": [
                {
                    "name": service_name,
                    "methods": ["list" + pascal_case(tail)],
                }
            ],
        }

    return {
        "name": module_name,
        "moduleKey": key,
        "version": "1.0.0",
        "enabled": True,
        "description": options.description or page_title + " module.",
        "dependencies": [],
        "pages": pages,
        "backend": backend,
        "frontend": {
            "entry": "./frontend/module.js",
            "stores": stores,
            "slots": [],
        },
        "permissions": {
            "read": "user",
            "write": "admin",
        },
        "metadata": {
            "category": options.menu_parent.lower(),
            "tags": ["generated", app_name.lower()],
        },
    }


def frontend_module_template(page_id, tail):
    method = "load" + pascal_case(tail)
    return """const moduleDefinition = {
  pages: [
    {
      id: '""" + page_id + """',
      name: '""" + title_case(page_id) + """',
      component: '""" + page_id + """Page'
    }
  ],

  onOpen: async (pageId, store) => {
    if (pageId === '""" + page_id + """' && store.getState().""" + method + """) {
      await store.getState().""" + method + """();
    }
  },

  onFocus: async () => {},

  onBlur: async (_pageId, store) => {
    if (store.getState().pausePolling) {
      store.getState().pausePolling();
    }
  },

  dispose: async (_pageId, store) => {
    if (store.getState().cleanup) {
      store.getState().cleanup();
    }
  }
};

module.exports = moduleDefinition;
"""


def page_template(component_name, title):
    return """const React = require('@platform/react');

const """ + component_name + """ = ({ store }) => {
  const items = store((state) => state.items || []);
  const loading = store((state) => state.loading);
  const error = store((state) => state.error);

  React.useEffect(() => {
    return () => {};
  }, [store]);

  if (loading) {
    return <div>Loading...</div>;
  }

  if (error) {
    return <div>{error}</div>;
  }

  return (
    <div className="card">
      <h1>""" + title + """</h1>
      <p className="content-copy">Generated runtime page.</p>
      <div>{items.length} item(s)</div>
    </div>
  );
};

module.exports = """ + component_name + """;
"""


def store_template(store_name, tail):
    method = "load" + pascal_case(tail)
    route_path = "/api/" + kebab_case(tail)
    return """const { createRuntimeStore, fetchJson } = require('@platform/services');

function create""" + store_name + """() {
  return createRuntimeStore((set, get) => ({
    items: [],
    loading: false,
    error: null,
    pollingInterval: null,

    """ + method + """: async () => {
      set({ loading: true, error: null });
      try {
        const result = await fetchJson('""" + route_path + """');
        set({ items: result.data || [], loading: false });
      } catch (error) {
        set({ error: error.message, loading: false });
      }
    },

    startPolling: () => {
      const pollingInterval = setInterval(() => {
        get().""" + method + """().catch(function ignorePollingError() {});
      }, 5000);
      set({ pollingInterval });
    },

    pausePolling: () => {
      const state = get();
      if (state.pollingInterval) {
        clearInterval(state.pollingInterval);
        set({ pollingInterval: null });
      }
    },

    cleanup: () => {
      get().pausePolling();
      set({ items: [], error: null });
    }
  }));
}

module.exports = create""" + store_name + """;
"""


def service_template(tail):
    method = "list" + pascal_case(tail)
    return """async function """ + method + """() {
  try {
    return {
      success: true,
      data: []
    };
  } catch (error) {
    return {
      success: false,
      error: error.message
    };
  }
}

module.exports = {
  """ + method + """
};
"""


def routes_template(tail):
    method = "list" + pascal_case(tail)
    route_path = "/api/" + kebab_case(tail)
    return """const express = require('express');
const router = express.Router();
const service = require('./service');

router.get('""" + route_path + """', async (req, res) => {
  try {
    if (!req.user) {
      return res.status(401).json({ error: 'Unauthorized' });
    }

    const result = await service.""" + method + """();
    if (!result || result.success !== true) {
      return res.status(500).json({
        error: result && result.error ? result.error : 'Failed to load data'
      });
    }

    return res.status(200).json(result);
  } catch (error) {
    return res.status(500).json({ error: error.message });
  }
});

module.exports = router;
"""


def add_descriptor_module(workspace, changes, app_name, module_name):
    descriptor = workspace.load_descriptor(app_name)
    modules = descriptor.setdefault("modules", [])
    exists = False
    for entry in modules:
        if isinstance(entry, str) and entry == module_name:
            exists = True
        elif isinstance(entry, dict) and (entry.get("name") == module_name or entry.get("directory") == module_name):
            exists = True
    if not exists:
        modules.append(
            {
                "name": module_name,
                "directory": module_name,
                "publish": True,
                "enabled": True,
            }
        )
    changes.write_json(workspace.descriptor_path(app_name), descriptor, overwrite=True)


def cmd_add_module(args):
    workspace = Workspace.discover(args.workspace)
    app_name, tail = parse_module_name(args.module)
    if not workspace.find_app(app_name):
        raise CliError("Unknown app '" + app_name + "'.")

    page_id = args.page_id or pascal_case(tail)
    page_title = args.title or title_case(tail)
    module_root = workspace.modules_root(app_name) / args.module
    changes = ChangeSet(args.dry_run, args.force)
    manifest = create_module_manifest(args.module, page_id, page_title, args)

    changes.write_json(module_root / "module.json", manifest, overwrite=False)
    changes.write_text(module_root / "frontend" / "module.js", frontend_module_template(page_id, tail))

    if not args.no_page:
        changes.write_text(
            module_root / "frontend" / "pages" / (page_id + "Page.jsx"),
            page_template(page_id + "Page", page_title),
        )

    if args.store:
        store_name = pascal_case(tail) + "Store"
        changes.write_text(
            module_root / "frontend" / "stores" / (store_name + ".js"),
            store_template(store_name, tail),
        )

    if args.backend:
        changes.write_text(module_root / "backend" / "service.js", service_template(tail))
        changes.write_text(module_root / "backend" / "routes.js", routes_template(tail))

    add_descriptor_module(workspace, changes, app_name, args.module)
    changes.print_summary()
    return 0


def ensure_frontend_module(workspace, changes, module_name):
    app_name, tail = parse_module_name(module_name)
    root = workspace.module_root(module_name)
    frontend_module = root / "frontend" / "module.js"
    if not frontend_module.exists():
        changes.write_text(frontend_module, frontend_module_template(pascal_case(tail), tail))


def append_page(manifest, page):
    pages = manifest.setdefault("pages", [])
    for existing in pages:
        if existing.get("id") == page["id"]:
            return False
    pages.append(page)
    pages.sort(key=lambda item: (item.get("order", 0), item.get("id", "")))
    return True


def cmd_add_page(args):
    workspace = Workspace.discover(args.workspace)
    app_name, tail = parse_module_name(args.module)
    manifest_path = workspace.module_manifest_path(args.module)
    if not manifest_path.exists():
        raise CliError("Module does not exist: " + args.module)

    page_id = args.page_id or pascal_case(args.page)
    page_title = args.title or title_case(args.page)
    page = {
        "id": page_id,
        "pageType": page_id,
        "name": page_title,
        "title": page_title,
        "component": page_id + "Page.jsx",
        "menu": {"label": page_title, "parent": args.menu_parent},
        "multiInstance": True,
        "order": args.order,
    }
    changes = ChangeSet(args.dry_run, args.force)
    manifest = read_json(manifest_path)
    append_page(manifest, page)
    changes.write_json(manifest_path, manifest, overwrite=True)
    changes.write_text(
        workspace.module_root(args.module) / "frontend" / "pages" / (page_id + "Page.jsx"),
        page_template(page_id + "Page", page_title),
    )
    ensure_frontend_module(workspace, changes, args.module)
    changes.print_summary()
    return 0


def cmd_add_store(args):
    workspace = Workspace.discover(args.workspace)
    app_name, tail = parse_module_name(args.module)
    manifest_path = workspace.module_manifest_path(args.module)
    if not manifest_path.exists():
        raise CliError("Module does not exist: " + args.module)
    manifest = read_json(manifest_path)
    store_name = args.store_name or pascal_case(tail) + "Store"
    frontend = manifest.setdefault("frontend", {})
    frontend.setdefault("entry", "./frontend/module.js")
    stores = frontend.setdefault("stores", [])
    if not any((entry == store_name) or (isinstance(entry, dict) and entry.get("name") == store_name) for entry in stores):
        stores.append({"name": store_name, "pages": args.page or []})

    changes = ChangeSet(args.dry_run, args.force)
    changes.write_json(manifest_path, manifest, overwrite=True)
    changes.write_text(
        workspace.module_root(args.module) / "frontend" / "stores" / (store_name + ".js"),
        store_template(store_name, tail),
    )
    ensure_frontend_module(workspace, changes, args.module)
    changes.print_summary()
    return 0


def upsert_backend_metadata(manifest, tail):
    backend = manifest.setdefault("backend", {})
    routes = backend.setdefault("routes", [])
    services = backend.setdefault("services", [])
    route_path = "/api/" + kebab_case(tail)
    service_name = module_key(tail) + "Service"
    method = "list" + pascal_case(tail)
    if not any(isinstance(route, dict) and route.get("path") == route_path for route in routes):
        routes.append({"path": route_path, "methods": ["GET"], "permissions": ["read"]})
    if not any(
        (service == service_name)
        or (isinstance(service, dict) and service.get("name") == service_name)
        for service in services
    ):
        services.append({"name": service_name, "methods": [method]})


def cmd_add_backend(args):
    workspace = Workspace.discover(args.workspace)
    app_name, tail = parse_module_name(args.module)
    manifest_path = workspace.module_manifest_path(args.module)
    if not manifest_path.exists():
        raise CliError("Module does not exist: " + args.module)
    manifest = read_json(manifest_path)
    upsert_backend_metadata(manifest, tail)

    changes = ChangeSet(args.dry_run, args.force)
    changes.write_json(manifest_path, manifest, overwrite=True)
    root = workspace.module_root(args.module)
    changes.write_text(root / "backend" / "service.js", service_template(tail))
    changes.write_text(root / "backend" / "routes.js", routes_template(tail))
    changes.print_summary()
    return 0


def cmd_add_include(args):
    workspace = Workspace.discover(args.workspace)
    if not workspace.find_app(args.app):
        raise CliError("Unknown app '" + args.app + "'.")
    if not args.allow_missing and not workspace.module_exists(args.module):
        raise CliError("Included module does not exist: " + args.module)

    descriptor = workspace.load_descriptor(args.app)
    includes = descriptor.setdefault("includes", [])
    if not any(isinstance(entry, dict) and entry.get("module") == args.module for entry in includes):
        record = {"module": args.module}
        if args.reason:
            record["reason"] = args.reason
        includes.append(record)

    changes = ChangeSet(args.dry_run, args.force)
    changes.write_json(workspace.descriptor_path(args.app), descriptor, overwrite=True)
    changes.print_summary()
    return 0


def cmd_add_dependency(args):
    workspace = Workspace.discover(args.workspace)
    if not workspace.module_exists(args.module):
        raise CliError("Module does not exist: " + args.module)
    if not args.allow_missing and not workspace.module_exists(args.dependency):
        raise CliError("Dependency module does not exist: " + args.dependency)
    manifest_path = workspace.module_manifest_path(args.module)
    manifest = read_json(manifest_path)
    dependencies = manifest.setdefault("dependencies", [])
    labels = {dependency_label(item) for item in dependencies}
    if args.dependency not in labels:
        dependencies.append(args.dependency)

    changes = ChangeSet(args.dry_run, args.force)
    changes.write_json(manifest_path, manifest, overwrite=True)
    changes.print_summary()
    return 0


def runtime_summary(manifest):
    active_modules = manifest.get("activeModules") or manifest.get("modules") or []
    published_modules = manifest.get("publishedModules") or manifest.get("modules") or []
    return {
        "currentSpec": manifest.get("currentSpec"),
        "currentSpecSource": manifest.get("currentSpecSource"),
        "activeSources": manifest.get("activeSources") or manifest.get("runtimeSearchOrder") or [],
        "activeModules": [item.get("name") for item in active_modules if isinstance(item, dict)],
        "publishedModules": [item.get("name") for item in published_modules if isinstance(item, dict)],
        "blockedModules": [
            {"name": item.get("name"), "reason": item.get("reason")}
            for item in manifest.get("blockedModules", [])
            if isinstance(item, dict)
        ],
        "includedModules": manifest.get("includedModules", []),
        "dependencyIncludedModules": manifest.get("dependencyIncludedModules", []),
        "effectiveModules": manifest.get("effectiveModules", {}),
        "pages": [item.get("id") for item in manifest.get("pages", []) if isinstance(item, dict)],
        "navigation": manifest.get("navigation", []),
        "backendLoadOrder": manifest.get("backendLoadOrder", []),
    }


def print_json_or_lines(payload, as_json):
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    for key, value in payload.items():
        if isinstance(value, (list, dict)):
            print(key + ": " + json.dumps(value, indent=2))
        else:
            print(key + ": " + str(value))


def cmd_inspect_runtime(args):
    workspace = Workspace.discover(args.workspace)
    manifest_path = workspace.runtime_manifest_path(args.mode)
    if not manifest_path.exists():
        raise CliError("Runtime manifest not found: " + str(manifest_path))
    print_json_or_lines(runtime_summary(read_json(manifest_path)), args.json)
    return 0


def module_status_from_runtime(workspace, module_name, mode):
    path = workspace.runtime_manifest_path(mode)
    if not path.exists():
        return None
    manifest = read_json(path)
    for module_record in manifest.get("activeModules", []):
        if module_record.get("name") == module_name:
            return {"status": "active", "reason": None}
    for module_record in manifest.get("blockedModules", []):
        if module_record.get("name") == module_name:
            return {"status": "blocked", "reason": module_record.get("reason")}
    for module_record in manifest.get("modules", []):
        if module_record.get("name") == module_name:
            return {"status": "published", "reason": None}
    return None


def cmd_inspect_module(args):
    workspace = Workspace.discover(args.workspace)
    manifest = workspace.load_module_manifest(args.module)
    payload = {
        "module": args.module,
        "sourcePath": str(workspace.module_manifest_path(args.module)),
        "runtimeStatus": module_status_from_runtime(workspace, args.module, args.mode),
        "moduleJson": manifest,
    }
    print_json_or_lines(payload, args.json)
    return 0


def cmd_list_apps(args):
    workspace = Workspace.discover(args.workspace)
    root_app, root_source = workspace.root_app()
    apps = []
    for app_definition in workspace.app_definitions():
        name = normalize_app_name(app_definition.get("prefix") or app_definition.get("local"))
        apps.append(
            {
                "app": name,
                "localFolder": app_definition.get("localFolder"),
                "root": name == root_app,
            }
        )
    payload = {"rootApp": root_app, "rootAppSource": root_source, "apps": apps}
    print_json_or_lines(payload, args.json)
    return 0


def cmd_list_modules(args):
    workspace = Workspace.discover(args.workspace)
    modules = []
    for module_record in workspace.iter_descriptor_modules():
        status = module_status_from_runtime(workspace, module_record["name"], args.mode)
        modules.append(
            {
                "name": module_record["name"],
                "app": module_record["app"],
                "directory": module_record["directory"],
                "publish": module_record["publish"],
                "enabled": module_record["enabled"],
                "runtimeStatus": status,
            }
        )
    print_json_or_lines({"modules": modules}, args.json)
    return 0


def cmd_list_pages(args):
    workspace = Workspace.discover(args.workspace)
    if args.active:
        manifest_path = workspace.runtime_manifest_path(args.mode)
        if not manifest_path.exists():
            raise CliError("Runtime manifest not found: " + str(manifest_path))
        pages = read_json(manifest_path).get("pages", [])
    else:
        pages = []
        for module_record in workspace.iter_descriptor_modules():
            manifest_path = workspace.module_manifest_path(module_record["name"])
            if manifest_path.exists():
                manifest = read_json(manifest_path)
                for page in manifest.get("pages", []):
                    pages.append(
                        {
                            "module": module_record["name"],
                            "id": page.get("id"),
                            "title": page.get("title") or page.get("name"),
                            "menu": page.get("menu"),
                        }
                    )
    print_json_or_lines({"pages": pages}, args.json)
    return 0


def cmd_doctor(args):
    workspace = Workspace.discover(args.workspace)
    root_app, root_source = workspace.root_app()
    descriptors = list(workspace.iter_descriptor_modules())
    runtime = workspace.runtime_folder()
    payload = {
        "workspace": str(workspace.root),
        "spec": str(workspace.spec_path),
        "localProperties": str(workspace.local_properties_path)
        if workspace.local_properties_path.exists()
        else None,
        "rootApp": root_app,
        "rootAppSource": root_source,
        "runtimeFolder": str(runtime),
        "runtimeFolderExists": runtime.exists(),
        "appCount": len(workspace.app_definitions()),
        "descriptorModuleCount": len(descriptors),
        "stagingRuntimeManifest": str(workspace.runtime_manifest_path("development")),
        "stagingRuntimeManifestExists": workspace.runtime_manifest_path("development").exists(),
    }
    print_json_or_lines(payload, args.json)
    return 0


def npm_command():
    return "npm.cmd" if os.name == "nt" else "npm"


def shell_join(command):
    return " ".join(shlex.quote(str(part)) for part in command)


def run_npm_script(workspace, script, script_args=None, dry_run=False):
    runtime = workspace.runtime_folder()
    if not runtime.exists():
        raise CliError("Runtime folder not found: " + str(runtime))
    command = [npm_command(), "run", script]
    if script_args:
        command.append("--")
        command.extend(script_args)
    print(shell_join(command) + "  # cwd=" + str(runtime))
    if dry_run:
        return
    result = subprocess.run(command, cwd=str(runtime))
    if result.returncode != 0:
        raise CliError("Command failed: " + shell_join(command))


def run_npm_scripts(workspace, scripts, dry_run=False):
    for script in scripts:
        run_npm_script(workspace, script, dry_run=dry_run)


def get_browser_url(workspace):
    configured = workspace.local_properties.get("electron.start.url")
    if configured:
        return configured
    port = workspace.local_properties.get("server.port") or "4100"
    return "http://localhost:" + str(port)


def schedule_browser_open(url, delay, dry_run=False):
    print("Open browser: " + url + " after " + str(delay) + "s")
    if dry_run:
        return
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import time, webbrowser; "
                "time.sleep(float(__import__('sys').argv[1])); "
                "webbrowser.open(__import__('sys').argv[2])"
            ),
            str(delay),
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def cmd_prepare(args):
    workspace = Workspace.discover(args.workspace)
    run_npm_scripts(workspace, ["prepare:runtime"], dry_run=args.dry_run)
    return 0


def cmd_validate(args):
    workspace = Workspace.discover(args.workspace)
    scripts = args.script or ["validate:precedence", "validate:phase1", "validate:phase2"]
    run_npm_scripts(workspace, scripts, dry_run=args.dry_run)
    return 0


def cmd_test(args):
    workspace = Workspace.discover(args.workspace)
    run_npm_scripts(workspace, ["test"], dry_run=args.dry_run)
    return 0


def cmd_config_init(args):
    workspace = Workspace.discover(args.workspace or os.getcwd())
    payload = load_cli_config()
    payload["defaultWorkspace"] = str(workspace.root)
    payload["installerDir"] = args.installer_dir
    payload["frameworkName"] = args.name or prompt_framework_name()
    save_cli_config(payload)
    print("Configured runtime workspace: " + str(workspace.root))
    print("Config file: " + str(cli_config_path()))
    return 0


def cmd_config_show(args):
    payload = load_cli_config()
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("Config file: " + str(cli_config_path()))
        if not payload:
            print("No config has been written. Run runtimekit config init --workspace <path>.")
        else:
            print_json_or_lines(payload, False)
    return 0


def cmd_config_clear(args):
    if args.dry_run:
        print("Would remove " + str(cli_config_path()))
        return 0
    if not args.yes:
        raise CliError("Clearing CLI config is destructive. Re-run with --yes or use --dry-run first.")
    clear_cli_config()
    print("Removed " + str(cli_config_path()))
    return 0


def cmd_run_browser(args):
    workspace = Workspace.discover(args.workspace)
    if args.production and args.server_only:
        raise CliError("Use either --production or --server-only, not both.")
    if args.production:
        script = "start"
    elif args.server_only:
        script = "server"
    else:
        script = "dev"
    browser_url = get_browser_url(workspace)
    print("Browser URL: " + browser_url)
    if args.open:
        schedule_browser_open(browser_url, args.open_delay, dry_run=args.dry_run)
    run_npm_script(workspace, script, dry_run=args.dry_run)
    return 0


def cmd_run_electron(args):
    workspace = Workspace.discover(args.workspace)
    run_npm_script(workspace, "electron", dry_run=args.dry_run)
    return 0


def build_release_args(args):
    release_args = []
    if getattr(args, "config", None):
        release_args.append("--config=" + args.config)
    if getattr(args, "output", None):
        release_args.append("--output=" + args.output)
    if getattr(args, "platform", None):
        release_args.append("--platform=" + args.platform)
    if getattr(args, "arch", None):
        release_args.append("--arch=" + args.arch)
    for target in getattr(args, "target", None) or []:
        release_args.append("--target=" + target)
    if getattr(args, "targets", None):
        release_args.append("--targets=" + args.targets)
    return release_args


def cmd_release_prepare(args):
    workspace = Workspace.discover(args.workspace)
    run_npm_script(workspace, "release:prepare", build_release_args(args), dry_run=args.dry_run)
    return 0


def collect_release_artifacts(workspace, output_dir, installer_dir, dry_run=False):
    output_root = resolve_workspace_path(workspace, output_dir) if output_dir else workspace.default_release_output_root()
    source_root = output_root / "make"
    if not source_root.exists() and output_root.exists():
        source_root = output_root

    target_root = workspace.installer_root(installer_dir)
    print("Collect release artifacts: " + str(source_root) + " -> " + str(target_root))

    if not source_root.exists():
        if dry_run:
            return
        raise CliError("Release output was not found at " + str(source_root))

    files = [path for path in source_root.rglob("*") if path.is_file()]
    if not files:
        print("No release artifact files found under " + str(source_root))
        return

    for source_file in files:
        relative = source_file.relative_to(source_root)
        copy_file(source_file, target_root / relative, dry_run=dry_run)


def cmd_release_make(args):
    workspace = Workspace.discover(args.workspace)
    run_npm_script(workspace, "release:make", build_release_args(args), dry_run=args.dry_run)
    if not args.no_collect:
        collect_release_artifacts(workspace, args.output, args.installer_dir, dry_run=args.dry_run)
    return 0


def remove_dependency_references(workspace, module_name, changes):
    for module_record in list(workspace.iter_descriptor_modules()):
        if module_record["name"] == module_name:
            continue
        manifest_path = workspace.module_manifest_path(module_record["name"])
        if not manifest_path.exists():
            continue
        manifest = read_json(manifest_path)
        dependencies = manifest.get("dependencies")
        if not isinstance(dependencies, list):
            continue
        updated = [item for item in dependencies if dependency_label(item) != module_name]
        if len(updated) != len(dependencies):
            manifest["dependencies"] = updated
            changes.write_json(manifest_path, manifest, overwrite=True)


def remove_include_references(workspace, module_name, changes):
    for app_definition in workspace.app_definitions():
        app_name = app_definition.get("prefix") or app_definition.get("local")
        if not app_name:
            continue
        descriptor_path = workspace.descriptor_path(app_name)
        if not descriptor_path.exists():
            continue
        descriptor = read_json(descriptor_path)
        includes = descriptor.get("includes")
        if not isinstance(includes, list):
            continue
        updated = []
        for include in includes:
            include_name = include if isinstance(include, str) else include.get("module") or include.get("name")
            if include_name != module_name:
                updated.append(include)
        if len(updated) != len(includes):
            descriptor["includes"] = updated
            changes.write_json(descriptor_path, descriptor, overwrite=True)


def remove_module_from_descriptor(workspace, module_name, changes):
    app_name, _ = parse_module_name(module_name)
    descriptor = workspace.load_descriptor(app_name)
    modules = descriptor.get("modules", [])
    updated = []
    removed = False
    for entry in modules:
        if isinstance(entry, str):
            matches = entry == module_name
        else:
            matches = entry.get("name") == module_name or entry.get("directory") == module_name
        if matches:
            removed = True
        else:
            updated.append(entry)
    if removed:
        descriptor["modules"] = updated
        changes.write_json(workspace.descriptor_path(app_name), descriptor, overwrite=True)


def cmd_remove_module(args):
    require_yes(args, "Removing a module")
    workspace = Workspace.discover(args.workspace)
    module_root = workspace.module_root(args.module)
    changes = ChangeSet(args.dry_run, args.force)

    if not args.keep_references:
        remove_dependency_references(workspace, args.module, changes)
        remove_include_references(workspace, args.module, changes)

    remove_module_from_descriptor(workspace, args.module, changes)
    changes.print_summary()

    if args.keep_files:
        print("Keeping module files at " + str(module_root))
    else:
        remove_path(module_root, dry_run=args.dry_run)

    return 0


def clean_release_paths(workspace):
    paths = [workspace.default_release_output_root()]
    apps_root = workspace.root / "apps"
    if apps_root.exists():
        for candidate in apps_root.glob("*/" + workspace.framework_folder_name() + "/release/out"):
            paths.append(candidate)
    unique = []
    seen = set()
    for path in paths:
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def clean_paths_for_targets(workspace, targets, installer_dir):
    paths = []
    for target in targets:
        if target == "staging":
            paths.append(workspace.runtime_folder() / "staging")
        elif target == "build":
            paths.append(workspace.runtime_folder() / "build-bin")
        elif target == "release":
            paths.extend(clean_release_paths(workspace))
        elif target == "installer":
            paths.append(workspace.installer_root(installer_dir))
    return paths


def cmd_clean(args):
    workspace = Workspace.discover(args.workspace)
    targets = list(args.target or [])
    if args.all:
        targets = ["staging", "build", "release", "installer"]
    if not targets:
        raise CliError("Choose --target <name> or --all. Supported targets: staging, build, release, installer.")

    require_yes(args, "Cleaning generated output")
    for path in clean_paths_for_targets(workspace, targets, args.installer_dir):
        remove_path(path, dry_run=args.dry_run)
    return 0


def add_common(parser):
    parser.add_argument("--workspace", default=None, help="Runtime spec workspace path.")


def add_write_options(parser):
    add_common(parser)
    parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing.")
    parser.add_argument("--force", action="store_true", help="Overwrite generated files when they exist.")


def add_release_options(parser):
    add_common(parser)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", help="Release config path passed to the runtime release script.")
    parser.add_argument("--output", help="Release output root passed to the runtime release script.")
    parser.add_argument("--platform", choices=["mac", "windows"], help="Release platform family.")
    parser.add_argument(
        "--target",
        action="append",
        choices=["windows-exe", "windows-msi", "mac-dmg", "mac-zip", "mac-pkg"],
        help="Release target. Can be provided more than once.",
    )
    parser.add_argument("--targets", help="Comma-separated release target list.")
    parser.add_argument("--arch", help="Release architecture, for example arm64 or x64.")


def prompt_framework_name():
    if sys.stdin.isatty():
        value = input("Framework display name [RuntimeKit]: ").strip()
        return value or "RuntimeKit"
    return "RuntimeKit"


def build_parser():
    parser = argparse.ArgumentParser(prog="runtimekit", description="Generic modular runtime framework CLI.")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser("doctor", help="Inspect workspace health.")
    add_common(doctor)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    config = subparsers.add_parser("config", help="Configure the installed CLI without hardcoded project paths.")
    config_sub = config.add_subparsers(dest="config_command")

    config_init = config_sub.add_parser("init", help="Remember a default runtime workspace.")
    add_common(config_init)
    config_init.add_argument("--installer-dir", default="installer")
    config_init.add_argument("--name", help="Framework display name stored in local CLI config.")
    config_init.set_defaults(func=cmd_config_init)

    config_show = config_sub.add_parser("show", help="Show CLI configuration.")
    config_show.add_argument("--json", action="store_true")
    config_show.set_defaults(func=cmd_config_show)

    config_clear = config_sub.add_parser("clear", help="Remove CLI configuration.")
    config_clear.add_argument("--dry-run", action="store_true")
    config_clear.add_argument("--yes", action="store_true")
    config_clear.set_defaults(func=cmd_config_clear)

    add = subparsers.add_parser("add", help="Generate or wire runtime framework pieces.")
    add_sub = add.add_subparsers(dest="add_command")

    add_module = add_sub.add_parser("module", help="Add a new app-owned module.")
    add_write_options(add_module)
    add_module.add_argument("module")
    add_module.add_argument("--backend", action="store_true")
    add_module.add_argument("--store", action="store_true")
    add_module.add_argument("--no-page", action="store_true")
    add_module.add_argument("--page-id")
    add_module.add_argument("--title")
    add_module.add_argument("--menu-parent", default="Operations")
    add_module.add_argument("--order", type=int, default=0)
    add_module.add_argument("--description")
    add_module.add_argument("--module-key")
    add_module.set_defaults(func=cmd_add_module)

    add_page = add_sub.add_parser("page", help="Add a page to an existing module.")
    add_write_options(add_page)
    add_page.add_argument("module")
    add_page.add_argument("page")
    add_page.add_argument("--page-id")
    add_page.add_argument("--title")
    add_page.add_argument("--menu-parent", default="Operations")
    add_page.add_argument("--order", type=int, default=0)
    add_page.set_defaults(func=cmd_add_page)

    add_store = add_sub.add_parser("store", help="Add a store to an existing module.")
    add_write_options(add_store)
    add_store.add_argument("module")
    add_store.add_argument("store_name", nargs="?")
    add_store.add_argument("--page", action="append", default=[])
    add_store.set_defaults(func=cmd_add_store)

    add_backend = add_sub.add_parser("backend", help="Add backend routes and service to a module.")
    add_write_options(add_backend)
    add_backend.add_argument("module")
    add_backend.set_defaults(func=cmd_add_backend)

    add_include = add_sub.add_parser("include", help="Include another module as an app feature.")
    add_write_options(add_include)
    add_include.add_argument("module")
    add_include.add_argument("--app", required=True)
    add_include.add_argument("--reason")
    add_include.add_argument("--allow-missing", action="store_true")
    add_include.set_defaults(func=cmd_add_include)

    add_dependency = add_sub.add_parser("dependency", help="Add a hard module dependency.")
    add_write_options(add_dependency)
    add_dependency.add_argument("module")
    add_dependency.add_argument("dependency")
    add_dependency.add_argument("--allow-missing", action="store_true")
    add_dependency.set_defaults(func=cmd_add_dependency)

    remove = subparsers.add_parser("remove", help="Remove generated or manually-created framework pieces.")
    remove_sub = remove.add_subparsers(dest="remove_command")

    remove_module = remove_sub.add_parser("module", help="Remove a module folder and descriptor references.")
    add_common(remove_module)
    remove_module.add_argument("module")
    remove_module.add_argument("--dry-run", action="store_true")
    remove_module.add_argument("--yes", action="store_true")
    remove_module.add_argument("--force", action="store_true")
    remove_module.add_argument("--keep-files", action="store_true")
    remove_module.add_argument("--keep-references", action="store_true")
    remove_module.set_defaults(func=cmd_remove_module)

    inspect = subparsers.add_parser("inspect", help="Inspect generated runtime state.")
    inspect_sub = inspect.add_subparsers(dest="inspect_command")

    inspect_runtime = inspect_sub.add_parser("runtime", help="Inspect staging or production runtime manifest.")
    add_common(inspect_runtime)
    inspect_runtime.add_argument("--mode", choices=["development", "production"], default="development")
    inspect_runtime.add_argument("--json", action="store_true")
    inspect_runtime.set_defaults(func=cmd_inspect_runtime)

    inspect_module = inspect_sub.add_parser("module", help="Inspect a source module and runtime status.")
    add_common(inspect_module)
    inspect_module.add_argument("module")
    inspect_module.add_argument("--mode", choices=["development", "production"], default="development")
    inspect_module.add_argument("--json", action="store_true")
    inspect_module.set_defaults(func=cmd_inspect_module)

    list_parser = subparsers.add_parser("list", help="List apps, modules, or pages.")
    list_sub = list_parser.add_subparsers(dest="list_command")

    list_apps = list_sub.add_parser("apps", help="List apps from spec.json.")
    add_common(list_apps)
    list_apps.add_argument("--json", action="store_true")
    list_apps.set_defaults(func=cmd_list_apps)

    list_modules = list_sub.add_parser("modules", help="List descriptor modules.")
    add_common(list_modules)
    list_modules.add_argument("--mode", choices=["development", "production"], default="development")
    list_modules.add_argument("--json", action="store_true")
    list_modules.set_defaults(func=cmd_list_modules)

    list_pages = list_sub.add_parser("pages", help="List source or active pages.")
    add_common(list_pages)
    list_pages.add_argument("--active", action="store_true")
    list_pages.add_argument("--mode", choices=["development", "production"], default="development")
    list_pages.add_argument("--json", action="store_true")
    list_pages.set_defaults(func=cmd_list_pages)

    prepare = subparsers.add_parser("prepare", help="Run the runtime prepare script.")
    add_common(prepare)
    prepare.add_argument("--dry-run", action="store_true")
    prepare.set_defaults(func=cmd_prepare)

    validate = subparsers.add_parser("validate", help="Run runtime validators.")
    add_common(validate)
    validate.add_argument("--dry-run", action="store_true")
    validate.add_argument("--script", action="append", help="Specific npm validation script to run.")
    validate.set_defaults(func=cmd_validate)

    test = subparsers.add_parser("test", help="Run the runtime npm test script.")
    add_common(test)
    test.add_argument("--dry-run", action="store_true")
    test.set_defaults(func=cmd_test)

    clean = subparsers.add_parser("clean", help="Remove generated staging, build, release, or installer output.")
    add_common(clean)
    clean.add_argument(
        "--target",
        action="append",
        choices=["staging", "build", "release", "installer"],
        help="Generated output target to clean. Can be provided more than once.",
    )
    clean.add_argument("--all", action="store_true", help="Clean staging, build, release, and installer output.")
    clean.add_argument("--installer-dir", default=None)
    clean.add_argument("--dry-run", action="store_true")
    clean.add_argument("--yes", action="store_true")
    clean.set_defaults(func=cmd_clean)

    run = subparsers.add_parser("run", help="Run the runtime application.")
    run_sub = run.add_subparsers(dest="run_command")

    run_browser = run_sub.add_parser("browser", help="Run the application for browser development.")
    add_common(run_browser)
    run_browser.add_argument("--dry-run", action="store_true")
    run_browser.add_argument(
        "--server-only",
        action="store_true",
        help="Run the prepared development server without webpack/watch processes.",
    )
    run_browser.add_argument(
        "--production",
        action="store_true",
        help="Build and run the production browser server.",
    )
    run_browser.add_argument(
        "--open",
        action="store_true",
        help="Open the application URL in the default browser after startup begins.",
    )
    run_browser.add_argument(
        "--open-delay",
        type=float,
        default=2.0,
        help="Seconds to wait before opening the browser when --open is used.",
    )
    run_browser.set_defaults(func=cmd_run_browser)

    run_electron = run_sub.add_parser("electron", help="Build and run the Electron application.")
    add_common(run_electron)
    run_electron.add_argument("--dry-run", action="store_true")
    run_electron.set_defaults(func=cmd_run_electron)

    release = subparsers.add_parser("release", help="Prepare or build runtime release installers.")
    release_sub = release.add_subparsers(dest="release_command")

    release_prepare = release_sub.add_parser(
        "prepare",
        help="Generate resolved release config snapshots.",
    )
    add_release_options(release_prepare)
    release_prepare.set_defaults(func=cmd_release_prepare)

    release_make = release_sub.add_parser(
        "make",
        help="Build release installers such as DMG, MSI, EXE, ZIP, or PKG.",
    )
    add_release_options(release_make)
    release_make.add_argument(
        "--installer-dir",
        default=None,
        help="Root-level folder where generated release artifacts are collected.",
    )
    release_make.add_argument(
        "--no-collect",
        action="store_true",
        help="Do not copy generated release artifacts into the installer folder.",
    )
    release_make.set_defaults(func=cmd_release_make)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except CliError as error:
        print("runtimekit: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
