# Blast Radius

RolledAndSmoked's entry for the [RWX Blast Radius track](https://www.joinbuilders.org/buildathon/tracks/blast-radius). The selector uses coverage collected from the unchanged sqlglot revision `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` to choose tests for a unified diff. It balances the selected tests using the scorer's two-second worker overhead.

## Approach

- The coverage map associates executed source lines and fixture reads with pytest node IDs. The collectors follow spawned worker functions, capture result serialization, and record class-scoped fixture reads.
- The selector reconstructs the changed source and checks syntax, encoding, and bytecode. Source inspection and imports contribute test candidates where a change can affect them.
- For isolated identity and equality edits, the selector uses the operand types observed on the pinned revision to prove equivalence per test. Custom comparison methods and missing observations keep the affected tests selected.
- The runtime gets test numbers and times from the grader's manifest. It reads only the manifest and patch files and writes JSON Lines to stdout. The submitted map is built solely from the pinned upstream checkout. The organizer's sample answers are used only with the local scorer.

## Reproduce

Download and extract the track's starter kit into a separate directory. Build this repo's container and mount the two official sample inputs.

```sh
docker build --platform linux/amd64 -t blast-radius .
docker run --rm --network none --read-only --user 65534:65534 \
  -v "$KIT/sample/tests.json:/data/tests.json:ro" \
  -v "$KIT/sample/commits:/data/commits:ro" blast-radius > output.jsonl
python3 "$KIT/score.py" "$KIT/sample" "$KIT/sample/answers.json" output.jsonl
```

For map regeneration, use the pinned checkout, the organizer's `requirements.lock`, and coverage.py 7.17.4. Put this repository's `tools` directory on `PYTHONPATH`. Turn on integration tests with `SKIP_INTEGRATION=0` and run the collectors from the upstream checkout.

| Collector | Environment | Compile the map from this repository |
| --- | --- | --- |
| `pytest tests -p trace_tests -n 5 --dist loadfile --timeout 150` | `TRACE_OUT` | `python tools/build_map.py UPSTREAM coverage-map.json.gz TRACE_OUT` |
| `pytest tests -p trace_types -n 2 --dist loadfile --timeout 150` | `TYPE_TRACE_ROOT=UPSTREAM`, `TYPE_TRACE_OUTPUT` | `python tools/build_type_map.py TYPE_TRACE_OUTPUT coverage-map.json.gz maps/comparison-types.json.gz` |
| `pytest tests/test_typing.py tests/test_sqlglotc.py -p trace_reads` | `READ_TRACE_OUTPUT` | `python tools/build_read_map.py READ_TRACE_OUTPUT maps/source-reads.json.gz` |
| `pytest tests/test_executor.py::TestExecutor::test_array_slice -p trace_fixture_scope` | `FIXTURE_SCOPE_OUTPUT` | Compress the resulting `main.json` to `maps/shared-fixtures.json.gz`. |

Use absolute paths and create each trace directory before launching pytest. Generate `maps/importers.json.gz` with `python tools/build_import_map.py UPSTREAM maps/importers.json.gz` and the initialization coverage with `python tools/collect_imports.py --source UPSTREAM --output maps/initialization-lines.json.gz`. Split the coverage map into ordered pieces with `split -b 175000 -d -a 3 coverage-map.json.gz maps/coverage.`. The Dockerfile rejoins the pieces.

On the 30 official samples, this version scores 136.195 simulated seconds with zero misses. The coverage-only version scored 182.975 seconds on the same sample.

This submission uses the organizer's pinned Python image and the MIT-licensed sqlglot project. The sqlglot source included in the map is covered by [its license](SQLGLOT-LICENSE). The selector and map tooling are distributed under [MIT](LICENSE).
