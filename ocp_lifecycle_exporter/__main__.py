import argparse
import logging
import sys

from .config import Config
from .kube import KubeClient
from .server import Exporter, serve


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ocp-lifecycle-exporter",
                                     description="Exports Red Hat lifecycle data for this cluster's OpenShift and operators.")
    parser.add_argument("--once", action="store_true", help="fetch everything once, print metrics and exit")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = Config.from_env()
    kube = KubeClient(cfg.kube_api_url, cfg.kube_token, insecure=cfg.kube_insecure)
    exporter = Exporter(cfg, kube)

    if args.once:
        exporter.refresh_api()
        exporter.refresh_cluster()
        exporter.render()
        sys.stdout.write(exporter.text)
        return 0 if exporter.catalog and exporter.cluster else 1

    serve(cfg, exporter)
    exporter.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
