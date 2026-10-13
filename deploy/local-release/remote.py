"""ECS entrypoint: verify package identity before invoking application handlers."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from common import ENVIRONMENTS, PROJECTS, remote_path, validate_bundle


def main(argv=None):
    """Dispatch a validated release or manual application rollback on its target ECS."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--project", required=True, choices=PROJECTS)
    parser.add_argument("--environment", required=True, choices=ENVIRONMENTS)
    parser.add_argument("--action", required=True, choices=["publish", "rollback"])
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--sha256")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        profile = json.loads(args.profile.read_text(encoding="utf-8"))
        if (profile.get("project"), profile.get("environment")) != (args.project, args.environment):
            raise ValueError("server profile identity does not match release selection")
        remote_path(profile.get("root"))
        if profile.get("kind") == "web":
            from web import deploy_web

            handler = deploy_web
        elif profile.get("kind") == "api":
            from api import deploy_api

            handler = deploy_api
        else:
            raise ValueError("invalid server release kind")
        if args.action == "publish":
            if not args.bundle or not args.sha256:
                raise ValueError("publish requires its package and transport checksum")
            with tempfile.TemporaryDirectory(prefix="juya-local-unpack-") as owned:
                bundle = Path(owned) / "bundle"
                manifest = validate_bundle(
                    args.bundle, args.sha256, args.project, args.environment, bundle
                )
                if manifest["kind"] != profile["kind"]:
                    raise ValueError("package kind does not match server profile")
                result = handler(bundle, profile, manifest, action="publish")
        else:
            result = handler(None, profile, None, action="rollback")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except KeyboardInterrupt:
        print("Server release interrupted", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print("Server release stopped: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
