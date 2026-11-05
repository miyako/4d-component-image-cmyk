#!/usr/bin/env python3
"""Prepare immutable, version-only release candidates without publishing them.

prepare writes local Git objects/ref and a full bundle, leaving HEAD/worktree
unchanged. restore checks out the exact candidate without resetting dirty work.
Only finalize may push, after signed-publication validation and production guards.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from ci_config import ROOT, load_config, release_tag, tag_version


REPOSITORY = "miyako/CMYK"
CANDIDATE_REF = "refs/heads/cmyk-release-candidate"
BOT = "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"
COAUTHOR = "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
BUMP_CHOICES = ("major", "minor", "patch", "promote")
HOST_CONTRACT = "tests/host/contract.json"
CLASS_DOC = "component/CMYK/Documentation/Classes/Converter.md"
VERSION_FILES = ("package.json", "template.json", HOST_CONTRACT, CLASS_DOC)
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
VERSION = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z", re.ASCII)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def parse_version(version):
    match = VERSION.fullmatch(version) if isinstance(version, str) else None
    require(match is not None, "version must be strict SemVer")
    result = match.groupdict(default="")
    require(all(not (item.isdigit() and len(item) > 1 and item.startswith("0"))
                for item in result["prerelease"].split(".")), "numeric prerelease identifiers cannot have leading zeros")
    for field in ("major", "minor", "patch"):
        result[field] = int(result[field])
    return result


def release_channel(tag):
    require(isinstance(tag, str), "release tag must be <SemVer>")
    prerelease = bool(parse_version(tag_version(tag))["prerelease"])
    return {"prerelease": prerelease, "latest": not prerelease}


def git(root, *arguments, data=None, env=None, label="Git operation", check=True):
    try:
        result = subprocess.run(["git", *arguments], cwd=root, input=data, capture_output=True,
                                timeout=180, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError(label + " could not complete") from None
    if check:
        require(result.returncode == 0, label + " failed")
    return result


def output(root, *arguments, **kwargs):
    return git(root, *arguments, **kwargs).stdout.decode("utf-8", errors="strict").strip()


def full_sha(value):
    require(isinstance(value, str) and SHA.fullmatch(value), "full normalized commit SHA required")
    return value


def safe_path(root, path):
    path = Path(path)
    candidate = path if path.is_absolute() else root / path
    require(not any(item.is_symlink() for item in (candidate, *candidate.parents)), "symlink paths are prohibited")
    candidate = candidate.resolve()
    require(candidate.is_relative_to(root / "build"), "candidate artifacts must be under root/build")
    return candidate


def regular(path):
    require(not any(item.is_symlink() for item in (path, *path.parents)) and path.is_file(),
            "required regular file missing or symlinked")


def digest(path):
    regular(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_object(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON object key")
            result[key] = value
        return result
    result = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    require(isinstance(result, dict), "JSON object required")
    return result


def repository(root):
    root = Path(root).absolute()
    require(not any(path.is_symlink() for path in (root, *root.parents)), "repository path cannot traverse symlinks")
    root = root.resolve()
    require(Path(output(root, "rev-parse", "--show-toplevel")).resolve() == root,
            "root must be the exact Git checkout root")
    allowed = {"https://github.com/miyako/CMYK", "https://github.com/miyako/CMYK.git",
               "git@github.com:miyako/CMYK", "git@github.com:miyako/CMYK.git",
               "ssh://git@github.com/miyako/CMYK", "ssh://git@github.com/miyako/CMYK.git"}
    urls = output(root, "config", "--get-all", "remote.origin.url", label="origin identity check").splitlines()
    require(len(urls) == 1 and urls[0] in allowed, "origin must identify exact miyako/CMYK")
    push = git(root, "config", "--get-all", "remote.origin.pushurl", check=False)
    require(push.returncode in (0, 1), "origin push URL check failed")
    push_urls = push.stdout.decode("utf-8").strip().splitlines()
    require(not push_urls or len(push_urls) == 1 and push_urls[0] in allowed,
            "origin push URL must identify exact miyako/CMYK")
    return root


def clean(root):
    require(not output(root, "status", "--porcelain=v1", "--untracked-files=all"),
            "checkout must be clean; dirty sources are never reset or cleaned")
    require(not output(root, "ls-files", "-u"), "unmerged index is prohibited")


def configuration(root):
    config = load_config(root, initialized=True, repository=REPOSITORY)
    require(config["repository"] == "CMYK" and config["namespace"] == "CMYK"
            and config["cli"] == "image-cmyk", "CMYK component identity must remain unchanged")
    parse_version(config["version"])
    return config


def planned(version, bump):
    require(bump in BUMP_CHOICES, "bump must be major, minor, patch or promote")
    parsed = parse_version(version)
    major, minor, patch = (parsed[field] for field in ("major", "minor", "patch"))
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    elif bump == "patch":
        patch += 1
    else:
        require(parsed["prerelease"], "promote requires a prerelease; version is already stable")
    target = f"{major}.{minor}.{patch}"
    return {"schema": 1, "repository": REPOSITORY, "previous_version": version, "version": target,
            "tag": release_tag(target), "bump": bump, **release_channel(release_tag(target))}


def plan(root, bump):
    root = repository(root)
    return planned(configuration(root)["version"], bump)


def patch_json(data, changes):
    """Replace only selected JSON value spans, retaining unrelated bytes/format."""
    decoder = json.JSONDecoder()
    source = data.decode("utf-8")
    spans = []
    seen = set()

    def whitespace(index):
        while index < len(source) and source[index] in " \t\r\n":
            index += 1
        return index

    def walk(index, path):
        index = whitespace(index)
        if path in changes:
            _, end = decoder.raw_decode(source, index)
            spans.append((index, end, json.dumps(changes[path], ensure_ascii=False, separators=(",", ":"))))
            seen.add(path)
            return end
        if source[index] == "{":
            index = whitespace(index + 1)
            if source[index] == "}":
                return index + 1
            while True:
                key, index = decoder.raw_decode(source, index)
                index = whitespace(index)
                require(source[index] == ":", "invalid JSON separator")
                index = whitespace(walk(index + 1, path + (key,)))
                if source[index] == "}":
                    return index + 1
                require(source[index] == ",", "invalid JSON object")
                index = whitespace(index + 1)
        if source[index] == "[":
            index, number = whitespace(index + 1), 0
            if source[index] == "]":
                return index + 1
            while True:
                index = whitespace(walk(index, path + (number,)))
                number += 1
                if source[index] == "]":
                    return index + 1
                require(source[index] == ",", "invalid JSON array")
                index = whitespace(index + 1)
        return decoder.raw_decode(source, index)[1]

    json_object(data)
    walk(0, ())
    require(seen == set(changes), "required version JSON field missing")
    for start, end, value in sorted(spans, reverse=True):
        source = source[:start] + value + source[end:]
    return source.encode("utf-8")


def version_changes(files, previous, version):
    """Recompute the complete, bounded version-only patch from original blobs."""
    result = {}
    for name in ("package.json", "template.json"):
        original = json_object(files[name])
        require(original.get("version") == previous, "base package/template versions are not synchronized")
        result[name] = patch_json(files[name], {("version",): version})
    package, template = (json_object(files[name]) for name in ("package.json", "template.json"))
    require(package.get("name") == "CMYK" and template.get("repository") == "CMYK"
            and template.get("namespace") == "CMYK" and template.get("cli") == "image-cmyk"
            and template.get("initialized") is True, "base candidate identity is not CMYK")
    contract = json_object(files[HOST_CONTRACT])
    old_stdout = json.dumps({"protocol": 1, "version": previous}, separators=(",", ":")) + "\n"
    new_stdout = json.dumps({"protocol": 1, "version": version}, separators=(",", ":")) + "\n"
    changes, found = {}, {"stdout": 0, "value": 0, "renamed": 0}
    for section in ("feature", "execution"):
        for index, assertion in enumerate(contract.get(section, {}).get("assertions", [])):
            pointer = assertion.get("path")
            prefix = (section, "assertions", index, "equals")
            if pointer == "/version/stdout":
                require(assertion.get("equals") == old_stdout, "stale host version stdout")
                changes[prefix] = new_stdout
                found["stdout"] += 1
            elif pointer == "/version/value":
                require(assertion.get("equals") == {"protocol": 1, "version": previous}, "stale host decoded version")
                changes[prefix + ("version",)] = version
                found["value"] += 1
            elif pointer == "/renamedExecutable":
                value = assertion.get("equals")
                require(isinstance(value, dict) and value.get("stdout") == old_stdout,
                        "stale renamed-executable version stdout")
                changes[prefix + ("stdout",)] = new_stdout
                found["renamed"] += 1
    require(found == {"stdout": 1, "value": 1, "renamed": 1}, "exact host version assertions are required")
    result[HOST_CONTRACT] = patch_json(files[HOST_CONTRACT], changes)
    if CLASS_DOC in files:
        source = files[CLASS_DOC].decode("utf-8")
        pattern = re.compile(r'(?m)^(\| `version\(\) : Object` \| `\{protocol:1,version:")([^"]+)("\}` \|)\r?$')
        matches = list(pattern.finditer(source))
        require(len(matches) <= 1, "ambiguous class version example")
        if matches:
            match = matches[0]
            require(match[2] == previous, "stale class version example")
            result[CLASS_DOC] = (source[:match.start(2)] + version + source[match.end(2):]).encode("utf-8")
    return result


def base_files(root, revision):
    paths = {}
    listing = git(root, "ls-tree", "-r", "-z", revision, "--", *VERSION_FILES).stdout
    for record in listing.split(b"\0"):
        if not record:
            continue
        fields, encoded_name = record.split(b"\t", 1)
        mode, kind, object_sha = fields.decode("ascii").split()
        name = encoded_name.decode("utf-8")
        require(name in VERSION_FILES and mode == "100644" and kind == "blob",
                "version surfaces must be regular tracked non-executable files")
        paths[name] = (mode, git(root, "cat-file", "blob", object_sha).stdout)
    require(all(name in paths for name in VERSION_FILES[:3]), "required tracked version surfaces missing")
    return paths


def remote_state(root, tag):
    main_ref, tag_ref = "refs/heads/main", "refs/tags/" + tag
    # A legacy "v"-prefixed tag for the same version also counts as already released.
    legacy_ref = "refs/tags/v" + tag_version(tag)
    result = {}
    for line in output(root, "ls-remote", "--refs", "origin", main_ref, tag_ref, legacy_ref,
                       label="refresh main/tag").splitlines():
        sha, ref = line.split("\t")
        full_sha(sha)
        require(ref in (main_ref, tag_ref, legacy_ref) and ref not in result, "ambiguous remote release refs")
        result[ref] = sha
    require(main_ref in result, "origin/main is missing")
    require(legacy_ref == tag_ref or legacy_ref not in result,
            "target version already has a legacy v-prefixed release tag")
    return result[main_ref], result.get(tag_ref)


def production_guard(root, metadata, *, resume=False):
    env = os.environ
    require(env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_REPOSITORY") == REPOSITORY
            and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and env.get("GITHUB_REF") == "refs/heads/main"
            and env.get("GITHUB_SHA") == metadata["base_sha"],
            "production release requires workflow_dispatch main at the exact base SHA")
    workflow_ref = env.get("GITHUB_WORKFLOW_REF")
    if workflow_ref:
        require(workflow_ref == REPOSITORY + "/.github/workflows/bump-release.yml@refs/heads/main",
                "production release must originate in main bump-release.yml")
    main, tag = remote_state(root, metadata["tag"])
    initial = main == metadata["base_sha"] and tag is None
    finalized = (resume and main == metadata.get("source_sha") and tag == metadata.get("source_sha"))
    require(initial or finalized, "origin/main moved or target tag already exists; no force/bypass permitted")
    return "finalized" if finalized else "pending"


def expected_commit(tree, base, version, timestamp):
    message = "Release CMYK " + version + "\n\n" + COAUTHOR + "\n"
    return (f"tree {tree}\nparent {base}\nauthor {BOT} {timestamp} +0000\n"
            f"committer {BOT} {timestamp} +0000\n\n" + message).encode("utf-8")


def verify_bundle(root, directory, metadata):
    bundle = directory / "candidate.bundle"
    require(metadata.get("bundle_sha256") == digest(bundle), "candidate bundle checksum mismatch")
    with bundle.open("rb") as stream:
        header = []
        while True:
            line = stream.readline(4097)
            require(line and len(line) <= 4096 and sum(map(len, header)) <= 65536, "invalid bundle header")
            if line == b"\n":
                break
            header.append(line)
    require(header[0] in (b"# v2 git bundle\n", b"# v3 git bundle\n")
            and not any(line.startswith(b"-") for line in header[1:]),
            "candidate requires a full bundle with all parents")
    git(root, "bundle", "verify", str(bundle), label="candidate bundle verification")
    heads = output(root, "bundle", "list-heads", str(bundle))
    require(heads == metadata["source_sha"] + " " + CANDIDATE_REF, "bundle must contain exactly the candidate ref")
    return bundle


def candidate_metadata(root, directory, expected_sha, expected_base=None):
    full_sha(expected_sha)
    directory = safe_path(root, directory)
    path = directory / "candidate.json"
    regular(path)
    require(path.stat().st_size <= 65536, "candidate metadata is oversized")
    metadata = json_object(path.read_bytes())
    require(type(metadata.get("schema")) is int and metadata["schema"] == 1
            and metadata.get("repository") == REPOSITORY and metadata.get("source_sha") == expected_sha
            and metadata.get("candidate_ref") == CANDIDATE_REF, "candidate identity/SHA mismatch")
    full_sha(metadata.get("base_sha"))
    full_sha(metadata.get("tree_sha"))
    require(expected_base is None or metadata["base_sha"] == full_sha(expected_base), "candidate base SHA mismatch")
    expected = planned(metadata.get("previous_version"), metadata.get("bump"))
    require(all(metadata.get(key) == value and type(metadata.get(key)) is type(value)
                for key, value in expected.items()), "candidate version/channel metadata mismatch")
    verify_bundle(root, directory, metadata)
    return metadata, directory


def validate_commit(root, metadata):
    expected_sha = metadata["source_sha"]
    require(output(root, "rev-list", "--parents", "-n", "1", expected_sha)
            == expected_sha + " " + metadata["base_sha"], "candidate must have exactly the declared base parent")
    originals = base_files(root, metadata["base_sha"])
    previous = json_object(originals["package.json"][1])["version"]
    require(previous == metadata["previous_version"], "candidate previous version differs from base")
    updates = version_changes({name: blob for name, (_, blob) in originals.items()}, previous, metadata["version"])
    names = output(root, "diff-tree", "--no-commit-id", "--name-only", "-r", expected_sha).splitlines()
    require(names == sorted(updates) and metadata.get("changed_paths") == names,
            "candidate contains changes outside the exact version-only patch")
    for name, content in updates.items():
        entry = output(root, "ls-tree", expected_sha, "--", name).split("\t", 1)[0].split()
        require(len(entry) == 3 and entry[0] == originals[name][0] and entry[1] == "blob",
                "candidate changed version-file type or mode")
        require(git(root, "show", expected_sha + ":" + name).stdout == content,
                "candidate version patch was altered")
    tree = output(root, "rev-parse", expected_sha + "^{tree}")
    timestamp = int(output(root, "show", "-s", "--format=%ct", metadata["base_sha"])) + 1
    require(tree == metadata["tree_sha"] and type(metadata.get("commit_timestamp")) is int
            and metadata["commit_timestamp"] == timestamp
            and git(root, "cat-file", "commit", expected_sha).stdout
            == expected_commit(tree, metadata["base_sha"], metadata["version"], timestamp),
            "candidate commit identity/message/date/tree is not deterministic")


def validate_candidate(root, directory, expected_sha, *, production=False):
    root = repository(root)
    metadata, _ = candidate_metadata(root, directory, expected_sha)
    require(output(root, "rev-parse", "HEAD") == expected_sha, "checkout is not the exact candidate SHA")
    clean(root)
    validate_commit(root, metadata)
    require(configuration(root)["version"] == metadata["version"], "candidate checkout version is not synchronized")
    if production:
        production_guard(root, metadata, resume=True)
    return metadata


def github_outputs(metadata):
    fields = {"sha": metadata["source_sha"], "base_sha": metadata["base_sha"], "tag": metadata["tag"],
              "version": metadata["version"], "prerelease": str(metadata["prerelease"]).lower()}
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("".join(key + "=" + value + "\n" for key, value in fields.items()))


def prepare(root, bump, base_sha, output_dir="build/release-candidate", *, production=False):
    root = repository(root)
    base_sha = full_sha(base_sha)
    clean(root)
    require(output(root, "rev-parse", "HEAD") == base_sha, "prepare HEAD must equal base SHA")
    require(output(root, "rev-parse", "--is-shallow-repository") == "false",
            "prepare requires full history (checkout fetch-depth: 0)")
    metadata = plan(root, bump)
    metadata["base_sha"] = base_sha
    if production:
        production_guard(root, metadata)
    directory = safe_path(root, output_dir)
    require(not directory.exists() or directory.is_dir() and not any(directory.iterdir()),
            "candidate output exists; restore/reuse it instead of computing a second bump")
    directory.mkdir(parents=True, exist_ok=True)
    originals = base_files(root, base_sha)
    updates = version_changes({name: blob for name, (_, blob) in originals.items()},
                              metadata["previous_version"], metadata["version"])
    index = directory / "candidate.index"
    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = str(index)
    try:
        git(root, "read-tree", base_sha, env=environment, label="initialize candidate index")
        for name in sorted(updates):
            blob = output(root, "hash-object", "-w", "--stdin", data=updates[name])
            git(root, "update-index", "--add", "--cacheinfo", originals[name][0], blob, name,
                env=environment, label="stage bounded version patch")
        tree = output(root, "write-tree", env=environment)
        timestamp = int(output(root, "show", "-s", "--format=%ct", base_sha)) + 1
        source_sha = output(root, "hash-object", "-t", "commit", "-w", "--stdin",
                            data=expected_commit(tree, base_sha, metadata["version"], timestamp))
        existing = output(root, "for-each-ref", "--format=%(objectname) %(refname)", CANDIDATE_REF)
        if existing:
            require(existing == source_sha + " " + CANDIDATE_REF,
                    "another local candidate exists; reuse exact candidate instead of overwriting")
        else:
            git(root, "update-ref", CANDIDATE_REF, source_sha, "0" * len(source_sha), label="create local candidate ref")
        bundle = directory / "candidate.bundle"
        git(root, "bundle", "create", str(bundle), CANDIDATE_REF, label="create full candidate bundle")
        metadata.update(source_sha=source_sha, tree_sha=tree, candidate_ref=CANDIDATE_REF,
                        commit_timestamp=timestamp, changed_paths=sorted(updates), bundle_sha256=digest(bundle))
        verify_bundle(root, directory, metadata)
        validate_commit(root, metadata)
        with (directory / "candidate.json").open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(metadata, indent=2) + "\n")
        github_outputs(metadata)
        return metadata
    finally:
        index.unlink(missing_ok=True)
        Path(str(index) + ".lock").unlink(missing_ok=True)


def restore(root, directory, expected_sha, expected_base=None):
    root = repository(root)
    clean(root)
    metadata, directory = candidate_metadata(root, directory, expected_sha, expected_base)
    bundle = directory / "candidate.bundle"
    git(root, "-c", "fetch.fsckObjects=true", "fetch", "--no-tags", "--no-write-fetch-head",
        str(bundle), CANDIDATE_REF + ":" + CANDIDATE_REF, label="restore candidate bundle")
    require(output(root, "rev-parse", CANDIDATE_REF) == expected_sha, "restored candidate ref mismatch")
    validate_commit(root, metadata)
    git(root, "-c", "advice.detachedHead=false", "checkout", "--detach", expected_sha,
        label="detach exact clean candidate")
    return validate_candidate(root, directory, expected_sha)


def finalize(root, directory, expected_sha):
    # Deferred to avoid a cycle: the publisher imports release_channel above.
    from publish_prerelease import publication_assets

    root = repository(root)
    metadata = validate_candidate(root, directory, expected_sha, production=True)
    publication_assets(root, metadata["tag"], expected_sha, tag_required=False)
    state = production_guard(root, metadata, resume=True)
    local_tag = git(root, "rev-parse", "--verify", "refs/tags/" + metadata["tag"], check=False)
    require(local_tag.returncode in (0, 128), "local release tag lookup failed")
    if local_tag.returncode == 0:
        require(local_tag.stdout.decode("ascii").strip() == expected_sha, "local release tag points elsewhere")
    else:
        git(root, "-c", "tag.gpgSign=false", "tag", "--no-sign", metadata["tag"], expected_sha,
            label="tag exact validated candidate")
    if state == "finalized":
        return {**metadata, "finalized": True, "resumed": True}
    git(root, "push", "--atomic", "--no-follow-tags", "origin", expected_sha + ":refs/heads/main",
        "refs/tags/" + metadata["tag"] + ":refs/tags/" + metadata["tag"],
        label="atomic release push rejected (protected/moved refs; no force or bypass)")
    require(remote_state(root, metadata["tag"]) == (expected_sha, expected_sha),
            "remote main/tag did not finalize exact candidate")
    return {**metadata, "finalized": True, "resumed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    planner = commands.add_parser("plan")
    planner.add_argument("--bump", choices=BUMP_CHOICES, required=True)
    preparer = commands.add_parser("prepare")
    preparer.add_argument("--bump", choices=BUMP_CHOICES, required=True)
    preparer.add_argument("--base-sha", required=True)
    preparer.add_argument("--output-dir", type=Path, default=Path("build/release-candidate"))
    preparer.add_argument("--production", action="store_true")
    restorer = commands.add_parser("restore")
    restorer.add_argument("--directory", type=Path, required=True)
    restorer.add_argument("--expected-sha", required=True)
    restorer.add_argument("--expected-base")
    finalizer = commands.add_parser("finalize")
    finalizer.add_argument("--directory", type=Path, required=True)
    finalizer.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = plan(args.root, args.bump)
        elif args.command == "prepare":
            result = prepare(args.root, args.bump, args.base_sha, args.output_dir, production=args.production)
        elif args.command == "restore":
            result = restore(args.root, args.directory, args.expected_sha, args.expected_base)
        else:
            result = finalize(args.root, args.directory, args.expected_sha)
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, TypeError, KeyError, UnicodeError, IndexError) as error:
        print("bump_release: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
