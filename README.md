# Blast Radius

RolledAndSmoked's entry for the [RWX Blast Radius track](https://www.joinbuilders.org/buildathon/tracks/blast-radius). The selector uses coverage collected from the unchanged sqlglot revision `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` to choose tests for a unified diff. It balances the selected tests using the scorer's two-second worker overhead.

## Approach

- Coverage and fixture reads on the pinned source establish the starting test set. The collector follows spawned workers and records result serialization.
- The selector checks each diff and uses the pinned operand types and values to recognize isolated Python expression changes that preserve behavior. Missing observations and custom comparison methods keep candidate tests selected.
- The original worker assignments cap simulated test time per patch. Tests that inspect the changed source can use any available capacity inside that cap.
- The image builds an indexed coverage map with separate compressed blocks for each source file. The selector loads coverage for the files it needs.
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
| `pytest tests -p profile_values -n 2 --dist loadfile --timeout 140` | `VALUE_OUT` | `python tools/profile_values.py VALUE_OUT maps/operand-values.json.gz` |

Use absolute paths and create each trace directory before launching pytest. Add this repository to `PYTHONPATH` when collecting operand values. Split the coverage map into ordered pieces with `split -b 175000 -d -a 3 coverage-map.json.gz maps/coverage.`. The Dockerfile rejoins and indexes the pieces.

On the 30 official samples, this practice version scores 130.198 simulated seconds with zero misses. The coverage-only version scored 182.975 seconds on the same sample. The cost cap can leave additional tests unselected for unusual import-time changes.

This submission uses the organizer's pinned Python image and the MIT-licensed sqlglot project. The sqlglot source included in the map is covered by [its license](SQLGLOT-LICENSE). The selector and map tooling are distributed under [MIT](LICENSE).
