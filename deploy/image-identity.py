#!/usr/bin/env python3
"""Validate a saved image by config digest across Docker image stores (stdlib only)."""

import hashlib
import json
import re
import sys
import tarfile


def image_identity(path, image):
    # Read metadata without extracting paths or buffering image layers in memory.
    metadata = {}
    with tarfile.open(path, "r|*") as archive:
        for member in archive:
            if not member.isfile() or member.size > 1024 * 1024:
                continue
            if member.name.endswith(".json") or member.name.startswith("blobs/sha256/"):
                if member.name in metadata:
                    raise ValueError("duplicate image metadata")
                metadata[member.name] = archive.extractfile(member).read()

    records = json.loads(metadata["manifest.json"])
    selected = [record for record in records if image in record.get("RepoTags", [])]
    if len(selected) != 1:
        raise ValueError("expected exactly one image with requested tag")
    config_path = selected[0]["Config"]
    config_bytes = metadata[config_path]
    config_id = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    if config_path not in (config_id[7:] + ".json", "blobs/sha256/" + config_id[7:]):
        raise ValueError("config bytes do not match archive digest")
    config = json.loads(config_bytes)
    if (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
        raise ValueError("expected Linux/amd64 image")

    accepted = {config_id}
    # containerd reports the OCI manifest digest; classic Docker reports config ID.
    index = json.loads(metadata.get("index.json", b'{"manifests":[]}'))

    def matches_config(descriptor, ancestors):
        digest = descriptor.get("digest", "")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError("invalid manifest digest")
        if digest in ancestors or len(ancestors) > 8:
            raise ValueError("invalid image index graph")
        raw = metadata.get("blobs/sha256/" + digest[7:])
        if raw is None or "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("manifest bytes do not match archive digest")
        manifest = json.loads(raw)
        matched = manifest.get("config", {}).get("digest") == config_id
        for child in manifest.get("manifests", []):
            if matches_config(child, ancestors | {digest}):
                matched = True
        if matched:
            accepted.add(digest)
        return matched

    for descriptor in index.get("manifests", []):
        matches_config(descriptor, set())
    return config_id, accepted


def main():
    if len(sys.argv) not in (3, 5):
        print("usage: image-identity.py <archive> <tag> [<config-id> <loaded-id>]", file=sys.stderr)
        return 64
    try:
        config_id, accepted = image_identity(sys.argv[1], sys.argv[2])
        if len(sys.argv) == 5 and (sys.argv[3] != config_id or sys.argv[4] not in accepted):
            raise ValueError(
                "expected config="
                + sys.argv[3]
                + ", archive config="
                + config_id
                + ", loaded="
                + sys.argv[4]
            )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError) as error:
        print("image identity mismatch: " + str(error), file=sys.stderr)
        return 65
    print(config_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
