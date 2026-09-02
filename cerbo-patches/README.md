# Patches applied to the running Cerbo

Both live on the **rootfs** (`/opt`, `/usr/lib`) and are silently reverted by a
Venus firmware update or a Signal K upgrade. Re-apply after either. Backups are
under `/data/*-backup*`, and every script takes `--revert`.

| Script | Target | Why |
|---|---|---|
| `patch_vesmart.py` (in `/data`, source below) | `vesmart-server` | crash loop, [venus#1674](https://github.com/victronenergy/venus/issues/1674) |
| `patch_canbus.py` | `@canboat/canboatjs/dist/canbus.js` | N2K filters applied after decode |
| `setfilters.py` | `/data/conf/signalk/settings.json` | the exclude list that patch makes effective |

## canboatjs: filters run too late

Pipeline is `canbus` -> `CanboatJs` (decode) -> `N2kToSignalK` (map).
`n2k-signalk.js` calls `n2kMapper.toDelta(chunk)` and only **then** checks
`isFiltered()`, so every excluded PGN is fully decoded and mapped before being
thrown away. Configuring filters saves nothing.

`patch_canbus.py` moves the same predicate into `canbus.js` `onMessage`, right
after `parseCanId()` — cheap bit-math that already yields `.pgn` and `.src`, the
two fields `isFiltered` compares. Address claims (60928) are never filtered.

Measured on this boat 2026-09-02, socketcan `can0` at ~320 frames/s:

| Configuration | signalk-server %CPU |
|---|---|
| provider on, unpatched, 5 exclusions | 41% |
| provider off | 4% |
| provider on, patched, 35 exclusions | **11%** |

### Before upstreaming, this is NOT yet general

- **`useCanName`**: `isFiltered` compares `source.canName` when that option is
  set. At ingest the NAME is not known yet, so the patch compares `pgn.src`
  unconditionally. Correct here (the option is unset) — wrong in general. A real
  fix must leave canName filters to the late path, or resolve them via
  `candevice`'s address->NAME map.
- **Provider coverage**: only `canbus.js` (socketcan) is patched. ikonvert,
  ydwg02, actisense-serial and w2k-1 all bypass it. The general placement is the
  head of the shared `CanboatJs` decode stage.
- **Other consumers**: filtered frames no longer reach `canboatjs:rawoutput`
  listeners or NMEA2000-out. That is a behaviour change; upstream may want it
  behind an option.

A two-tier PR is the honest shape: (1) in `n2k-signalk.js`, evaluate the filter
on `chunk` before `toDelta` — universal, tiny, safe; (2) filter before decode in
the `CanboatJs` stage for the large win.
