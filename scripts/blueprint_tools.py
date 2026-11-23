"""Project the controller-validated tool grant onto the controlled role's lease files."""
import json
import re
from environment_leases import ROOT, fingerprint

SOURCE_FILES = {
    "Source": ("source.read", "source", "shipping-rule.json"),
    "Decisions": ("decisions.read", "decisions", "decisions.json"),
    "Cases": ("cases.read", "cases", "acceptance.json"),
}


FACTORY_FILES = {
    "Source": ("source.read", "source", "fixtures/self-development/task-summary.cjs"),
    "Decisions": ("decisions.read", "decisions", "fixtures/self-development/policy.json"),
    "Cases": ("cases.read", "cases", "docs/fct020-operator-evidence.json"),
}


def repository_rules(order):
    version = order["assignment"]["policyVersion"]
    if version in ("shipping/v1", "factory-summary/v1", "factory-summary/v2"):
        return None  # Original shipping snapshot and both frozen renderer profiles.
    if not re.fullmatch(r"[a-z][a-z0-9-]*/v[1-9][0-9]*", version):
        raise ValueError("Invalid repository policy version")
    project, edition = version.split("/")
    directory = ROOT / "src/Factory.Api/bin/Debug/net10.0/projects" / project
    path = directory / (edition+"-policy.json")
    if not path.is_file():
        path = directory / "factory-policy.json"
    manifest = path.read_text(encoding="utf-8")
    rules = json.loads(manifest)
    if rules["policy"]["version"] != version:
        raise ValueError("Unexpected installed repository policy")
    if rules.get("repositoryLayout") is None:
        return None
    grant = (order.get("context") or {}).get("blueprint") or {}
    if fingerprint(manifest) != grant.get("manifestHash"):
        raise ValueError("Repository layout differs from the admitted project policy")
    return rules


def repository_layout(order):
    rules = repository_rules(order)
    return rules["repositoryLayout"] if rules else None


def candidate_files(layout, candidate):
    if not layout.get("additionalWrites"):
        return {layout["source"]: candidate}
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate candidate path")
        return result
    files = json.loads(candidate, object_pairs_hook=unique)
    if (not isinstance(files,dict) or set(files) != {layout["source"], *layout["additionalWrites"]}
            or any(not isinstance(content,str) or not content or "\0" in content
                   or len(content.encode()) > 16384 for content in files.values())):
        raise ValueError("Candidate differs from the admitted write set")
    return files


def catalog(order):
    layout = repository_layout(order)
    if layout:
        return {part: (tool, key, layout[key]) for part, (tool, key, _) in SOURCE_FILES.items()}
    return FACTORY_FILES if order["assignment"]["policyVersion"] in ("factory-summary/v1","factory-summary/v2") else SOURCE_FILES


def lease_sources(order, operation):
    source = order["source"]
    context = order.get("context") or {}
    grant = context.get("blueprint")
    filespec = catalog(order)
    if grant is None:
        if filespec is FACTORY_FILES:
            raise ValueError("Factory-source lease requires the admitted blueprint")
        return {path:source[key] for _, key, path in filespec.values()}
    tools = grant["tools"]
    if operation not in tools:
        raise ValueError("Role tool is outside the admitted project grant")
    files = {}
    layout = repository_layout(order)
    extras = json.loads(source["additionalFiles"]) if layout else {}
    if layout and (not isinstance(extras,dict) or set(extras) != set(layout["additionalReads"] + layout.get("additionalWrites",[]))):
        raise ValueError("Supplemental sources differ from the installed layout")
    for document in context["documents"]:
        tool, key, path = filespec[document["part"]]
        content = source[key]
        supplemental = (layout.get("additionalWrites",[]) if document["part"] == "Source" else
                        layout["additionalReads"] if document["part"] == "Cases" else []) if layout else []
        if document["path"] in supplemental:
            path = document["path"]
            content = extras[path]
        if (tool not in tools or path != document["path"] or path in files
                or document["revision"] != order["assignment"]["sourceHash"]
                or fingerprint(content) != document["digest"]):
            raise ValueError("Source reference differs from the admitted role and revision")
        files[path] = content
    expected = {path for tool, _, path in filespec.values() if tool in tools}
    if layout and "cases.read" in tools:
        expected.update(layout["additionalReads"])
    if layout and "source.read" in tools:
        expected.update(layout.get("additionalWrites", []))
    if files.keys() != expected:
        raise ValueError("Missing source reference for an admitted read tool")
    return files
