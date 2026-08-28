#!/usr/bin/env bash
set -euo pipefail

python code/experiment.py --spec fluent_experiment.json --output-dir .
