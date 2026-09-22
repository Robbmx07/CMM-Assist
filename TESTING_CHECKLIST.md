# CMM-Gen Testing Checklist

Use this after running `launch.bat` (Windows) or `launch.sh` (macOS/Linux).
Work through it top to bottom -- each section builds on the one before it.

## 1. Install & smoke test

- [ ] `launch.bat` / `launch.sh` completes with no red `[ERROR]` lines.
- [ ] The smoke test table prints 13 features (8 planes, 2 cylinders, 1 cone,
      1 slot, 1 circle) for `tests/fixtures/sample_part.step`.
- [ ] The Streamlit visualizer opens at `http://localhost:8501` in your
      browser (the script launches it automatically after the smoke test).

## 2. Automated test suite (optional but recommended)

With the virtual environment active (`.venv\Scripts\activate` on Windows,
`source .venv/bin/activate` on macOS/Linux):

- [ ] `pip install pytest ruff mypy` (not in the runtime `requirements.txt`,
      only needed if you want to re-run these yourself)
- [ ] `pytest` -- 30 tests pass
- [ ] `ruff check cmm_gen/ tests/` -- no findings
- [ ] `mypy cmm_gen/` -- no findings

## 3. Visualizer walkthrough (CAD Features + Approach Vectors tabs)

- [ ] Leave the default STEP path (the sample part) and click
      **Run extraction**. Confirm the 3D scatter plot shows 13 points
      colored by feature type, and the table below matches.
- [ ] Pick a probe and machine from the sidebar dropdowns, click
      **Run extraction** again. Confirm the **Approach Vectors** tab shows
      "Reachable features: 12 / 13" and the plot's one red/brown line
      corresponds to the plate's bottom face (`PLN_005`) -- it's genuinely
      unreachable from a single top-side setup, not a bug.
- [ ] Point the STEP path at one of your own part files and repeat. Confirm
      feature counts and types look right for that geometry. If something
      looks wrong (a hole missing, a slot not detected), note which feature
      and its approximate geometry -- see **Known limitations** below.

## 4. CLI: GD&T extraction (needs `ANTHROPIC_API_KEY`)

```
set ANTHROPIC_API_KEY=sk-ant-...        (Windows)
export ANTHROPIC_API_KEY=sk-ant-...     (macOS/Linux)
python -m cmm_gen.cli extract-gdt --step your_part.step --blueprint your_print.pdf
```

- [ ] Every feature control frame visible on the blueprint appears in the
      output table (spot-check against the drawing by eye).
- [ ] Tolerance values and datum letters match the drawing.
- [ ] Check the **Mapped Feature** column: entries marked `UNMAPPED` mean
      CMM-Gen couldn't confidently tie that callout to a CAD feature --
      expected for callouts without legible basic dimensions on a busy
      print, but every hole/plane callout you'd expect to map should map.
      Unexpected `UNMAPPED` rows usually mean the basic dimensions on the
      print don't match the CAD nominal within 5mm -- check units
      (mm vs. inch) first.

## 5. CLI: full pipeline

```
python -m cmm_gen.cli generate --step your_part.step --blueprint your_print.pdf ^
    --out your_part.prg --probe HH-A-T5 --machine default
```

- [ ] Command exits 0 and prints `Wrote your_part.prg`.
- [ ] The "Unreachable features" and "Unmapped GD&T callouts" tables (if
      any print) make sense for the part -- e.g. underside features with no
      second setup, callouts with no basic dimensions.
- [ ] If it exits with "Only found N datum feature(s)": the blueprint needs
      a legible datum feature symbol (boxed letter) on at least three
      features for ASME Y14.5 alignment; this is a real print-legibility
      limit, not a bug to chase.

## 6. Before trusting output on the shop floor -- replace the placeholders

- [ ] Open `config/probe_library.yaml`. For each probe you'll actually use
      (`HH-A-T5`, `HA-TM-31`, `HP-TBe`, `HT-TM-MF`, or your own), replace the
      tip diameter, stylus length, index increment, and A/B angle range with
      the real datasheet values, then set `verified: true`. Until you do,
      every run prints a loud warning and reachability results shouldn't be
      trusted.
- [ ] Open `config/machine_envelope.yaml`. Replace the `default` machine's
      travel limits with your actual CMM's, add any fixture/tooling
      keep-out boxes, and set `verified: true`.
- [ ] Re-run the visualizer / CLI against a real part after updating both
      files and confirm the reachability results change sensibly (e.g. a
      smaller envelope should reject more features near the travel limits).

## 7. PC-DMIS import (the real test)

- [ ] Open a generated `.prg` file in the PC-DMIS editor (not just a text
      editor) and confirm it loads without syntax errors.
- [ ] Search the file for every `$$ VERIFY` / `'$$ VERIFY` comment -- these
      mark DIM types (`PROFILE`, `TOTAL RUNOUT`, `CONCENTRICITY`,
      `SYMMETRY`) and the Cypress Enable BASIC tolerance-check subroutine's
      COM property names that this project could not validate against a
      live PC-DMIS instance. Confirm each one against your installed
      version before relying on it.
- [ ] Step through the alignment block and confirm `ALIGNMENT/LEVEL`,
      `ROTATE`, and `ORIGIN` reference the datum features you expect, in
      A/B/C order.
- [ ] Run the routine in DCC mode on the machine (or simulate it if
      available) and confirm probe moves clear the part -- the collision
      check in this tool is a simplified bounding-box proxy, not a full
      solid-vs-solid sweep, so this is the step that actually catches a
      real crash risk the software might have missed.
- [ ] Compare a few measured dimensions against a known-good manual program
      or CMM report for the same part, if you have one.

## Known limitations (expected, not bugs)

- Hit-point placement (how many points, where) uses a reasonable default
  pattern per feature type, not the actual face boundary -- adjust hit
  count/spacing in the PC-DMIS editor if a pattern looks too sparse or dense
  for a given feature's size.
- Collision checking is a bounding-box proxy, not a full geometry sweep --
  step 7's on-machine (or simulated) run-through is what actually catches a
  real crash.
- Slot detection only recognizes obround (rounded-end) slots reconstructed
  from two matching half-cylindrical faces; unusual slot shapes will show up
  as a logged warning rather than a wrong feature.
