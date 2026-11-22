# Blast Radius

RolledAndSmoked's entry for the [RWX Blast Radius track](https://www.joinbuilders.org/buildathon/tracks/blast-radius). The selector uses coverage collected from the unchanged sqlglot revision `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` to choose tests for a unified diff. It balances the selected tests using the scorer's two-second worker overhead.

## Approach

- The map associates executed source lines and fixture reads with pytest node IDs. The collector follows tests into their spawned worker functions and captures result serialization.
- The selector checks each diff against the pinned source, widens the test set for changes to control flow or declarations, and falls back to the full suite when it cannot analyze a change reliably.
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

For map regeneration, use the pinned checkout, the organizer's `requirements.lock`, and coverage.py 7.17.4. Put this repository's `tools` directory on `PYTHONPATH`. Set `TRACE_OUT` to an existing absolute directory, turn on integration tests with `SKIP_INTEGRATION=0`, and run `python -m pytest tests -p trace_tests -n 5 --dist loadfile --timeout 150` from the upstream checkout. Then run `python tools/build_map.py UPSTREAM coverage-map.json.gz TRACE_OUT` from this repo. It also accepts multiple trace directories. The Dockerfile rejoins the ordered pieces in `maps/` to assemble the committed map.

This submission uses the organizer's pinned Python image and the MIT-licensed sqlglot project. The sqlglot source included in the map is covered by [its license](SQLGLOT-LICENSE). The selector and map tooling are distributed under [MIT](LICENSE).
