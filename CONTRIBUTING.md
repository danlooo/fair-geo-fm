# Contributing

Conventions for the processes and runtimes in this repository.

## Processes

- Each process is a user-defined process (UDP) served through the
  [openEO remote process definition extension](https://api.openeo.org/extensions/remote-process-definition/0.1.0).
  Callers set `namespace` to the process's `process_graph.json` in this repo.
- One directory per major model version, so versions can be used side by side:
  `processes/tessera_v2/`, `processes/tessera_v1/`.
- Put the major version in the process id, not in a parameter:
  `tessera_v2_embeddings`. UDPs have no version property, and a
  `model_version` parameter cannot change the process contract.
- Process ids are model-first, end with `_embeddings`, and carry no `infer_` or
  `load_` prefix. The suffix states the output type; a verb would collide with
  the generic `load_embeddings` and `ml_predict` processes. Say whether a
  process computes on demand or reads archived tiles in `summary` and
  `description`.
- One embedding per process graph: do not pack several models into one UDP.
- Use openEO-friendly file names with underscores. Process ids must match
  `^\w+$`, so no hyphens or dots.
- Inside a version directory, use role names rather than repeating the id:
  - `process_graph.json` — the UDP definition served as the remote namespace,
    maintained by hand
  - `udf.py` — UDF source, when the UDF is specific to this model
  - `example.py` — sample call
  - `README.md`
- Inline the UDF in `process_graph.json` so one URL resolves the whole process.
  If it grows too large to maintain inline, reference `udf.py` by URL from
  `run_udf` instead.
- Point `namespace` at a commit or tag, not at a branch, so stored jobs stay
  reproducible.
- Do not end URLs with a trailing `/` (namespace, `links`, documentation).
- Documentation is published at <https://danlooo.github.io/fair-geo-fm>. Put the
  link in the process `description` and in `links` (`rel: canonical`), and in
  the node `description` of copy-paste examples — that is what a user reads in
  the Web editor's Code tab.

> **Note:** the openEO Web editor does not retrieve metadata from the external
> `namespace` URL when a process graph is created. `summary`, `description`,
> `links` and the parameter descriptions are not shown there; the editor only
> displays the process graph that is pasted into it. Do not rely on them to
> communicate anything users must see — put it in the node `description` of
> examples, this README, or the documentation page instead.

## Runtimes

- Shared dependency archives live in `runtimes/`, never inside a process
  directory, and are reused across processes.
- One directory per interpreter/stack combination, named after both, containing
  only `requirements.txt`: `runtimes/py311-torch231/`. The target Python version
  is encoded in the name (`py311` -> 3.11) and the file may pin an index via
  `--extra-index-url`.
- `uv run scripts/create-python-dependency-archive.py --requirements
  runtimes/py311-torch231/requirements.txt --output-file
  runtimes/py311-torch231/dependencies.zip` writes that file; upload it to
  object storage and never overwrite a published archive.
- Pin `runtime` and `version` in `run_udf` and reference the archive from the
  process's `default_job_options` via `udf-dependency-archives`, so interpreter
  and archive move together.
- Do not serve archives from GitHub raw, releases, or LFS: raw is capped at
  100 MiB, releases and LFS redirect to hosts that are often not allowlisted,
  and LFS is bandwidth-metered. Use object storage.
