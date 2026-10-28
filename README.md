# Fixture Manager for Autodesk Fusion

Fixture Manager is a Fusion add-in for CAM workholding. It keeps a library of your zero point
plates, vises, chucks, hard jaws, soft jaw blanks and custom fixtures, each tracked by its Fusion
file's **URN**. In a CAM design it inserts or swaps the whole stack (plate → vise → jaws) and
**rigidly joins the stock to it**. Swap a 4" vise with 1.000" jaws for a 6" vise with 1.750" jaws
and the part moves to the new seat height, the setups' fixture lists are refreshed, and toolpaths
can be regenerated.

Highlights:

- **No configurations.** Each fixture is its own ordinary Fusion design, placed by named joint
  origins (`FM_*`), so the same library file works in every job.
- **Zero point plates:** stud patterns, plate positions and rotation on the plate.
- **Vises and centering chucks:** jaws slide to the stock width; chucks run 2, 3, 4 or 6 jaws,
  spaced automatically from one main jaw seat.
- **Hard jaws** (normal or reversed, stepped jaws with up to three seats) and **soft jaws**
  (local copies you cut, plus uncut soft jaw stock for the jaw-cutting setup).
- **Three stations per job file:** OP1 (the part goes to the fixture) and OP2 / OP3 (the
  fixture comes to the part, so each op cuts the previous op's rest stock). Each station has its
  own plate, vise, jaws and WCS.
- **CAM aware:** setups are linked to their station by name (`Op 1`, `Op 2`, `Op 3`) and keep
  their fixture and WCS selections across swaps.
- **Shareable library:** a plain JSON file that can live on a shared drive.

When you swap, the add-in deletes its own joints, replaces what changed inside its container, and
recreates the joints from the joint origin names. It never tries to repair a joint that a
replaced component broke, so a swap has the same result every time.

---

## 1. Install

**From a release (simplest):**

1. Download `FixtureManager-v<version>.zip` from the
   [Releases](../../releases) page and unzip it somewhere permanent. You get a `FixtureManager`
   folder.
2. In Fusion, open **Utilities > Add-Ins > Scripts and Add-Ins**, select the **Add-Ins** tab,
   click **+**, and choose the `FixtureManager` folder.
3. Select **FixtureManager** and click **Run**. Tick *Run on Startup* to load it every time.
4. A **FIXTURES** tab appears in the Design and Manufacture workspaces with **Apply / Swap
   Fixture**, **Register Fixture**, **Fixture Library**, **Publish Version** and **Check for
   Updates**.

**As a git clone (to get updates from inside Fusion):**

```bash
git clone https://github.com/ForefrontMFG/Fusion-Fixture-Manager.git FixtureManager
```

Add that folder in Scripts and Add-Ins as above. **Check for Updates** then fetches newer
versions from GitHub, lists their notes, and installs them (Stop and Run the add-in afterwards).
**Publish Version** is for maintainers of a fork: it commits, tags and pushes a new version to
the clone's own `origin`.

The library file defaults to `~/Documents/FixtureManager/fixture_library.json`. Change the path
in **Fixture Library** to share one library across several computers.

## 2. Prepare library files (once per fixture)

Model each piece of workholding as its own **non-configured** Fusion design, add the joint
origins below, save it, and run **Register Fixture** while it is the active document. Pick its
**Type**, give it a display name, and the URN plus its joint origins are stored in the library.
Re-register after you add, move or rename joint origins.

All joint origins have **Z pointing up**. Joint origins inside child components are found too.

| Type | Joint origins |
|---|---|
| **Zero point plate** | `FM_PlateMount` where the plate sits on the machine. `FM_WCS` at the top center of the plate (the work offset). One receiver per position and stud pattern on the top face, named `FM_ZP_<position>_<pattern>`, e.g. `FM_ZP_Center_96`, `FM_ZP_A_52` |
| **Vise** (body and master jaws) | `FM_Center`, `FM_BaseTop`, and `FM_JawSeat_A1` / `FM_JawSeat_A2` (plus `B1/B2` … for more positions) where each jaw bolts on, X pointing toward the center. One stud per pattern it can mount with, `FM_ZP_52` and/or `FM_ZP_96`, on the underside |
| **Centering chuck** | `FM_Center` on the chuck axis, ONE main jaw seat `FM_JawSeat_A1` on a jaw slot, and `FM_ZP_` studs. In Register enter the **Jaw counts** it can run, e.g. `3, 6`. The other seats are generated at Apply, evenly spaced (2 = 180°, 3 = 120°, …) |
| **Hard jaw** (one jaw) | `FM_Bolt` at the bolt interface, `FM_Grip` on the gripping edge (centered across the jaw), and `FM_GripRev` on the reversed edge if it can be flipped. Each grip sits where the part sits. Stepped chuck jaws add `FM_Grip2` / `FM_GripRev2` (jaw step 1) and `FM_Grip3` / `FM_GripRev3` (jaw step 2). The jaw faces from `FM_Bolt` toward `FM_Grip` |
| **Soft jaw blank** (one jaw) | `FM_Bolt`, `FM_Grip`; also enter the cut opening (spacer width) |
| **Fixture** (single mount point) | `FM_Mount` where the bottom center of the stock sits, plus `FM_ZP_<pattern>` studs if it goes on a plate |

Vises, chucks and their jaws are matched by the same **Jaw interface** text, for example "6in
Kurt". Jaws are only offered for a vise or chuck with a matching interface.

Notes:

- **Lock every child component.** If the joint origins are in the file's top level but the body
  is in a child component, that child must be *Ground to Parent* (or jointed to something that
  is), or it stays behind when the fixture is moved. **Register checks for this** and offers
  *Lock loose components*: it grounds the right component and saves a new version first.
- **A joint origin's offsets are in its own rotated frame.** A seat 60 mm from the center with its
  X turned 90° toward the center has offset X = −60 mm, not Y.
- **Files rigged with the AU2024 "Workflow Template" names** ("Stock Attachment" …) are
  recognized as single-mount fixtures.
- **Configured designs are refused.** Save the fixture as an ordinary design and register that.

## 3. Prepare a job file

- **Stock parameters:** `StockX`, `StockY`, `StockZ` (and `StockRadius` for round bar). `StockY`
  runs across the jaws. Center the stock sketch on `FM_Stock` so it grows evenly.
- **`FM_Stock`** (OP1): a joint origin at the **bottom center of the stock**, Z up.
- **`FM_OP2`** (OP2): a joint origin on the stock, flipped so **Z points down into the part**,
  offset `-(StockZ - OP2Inset)` from the stock bottom. `OP2Inset` is how far the stock sits in the
  OP2 jaws.
- **`FM_OP3`** (OP3): the mirror at the other end, Z offset `OP3Inset`, Z up.
- **Optional `StockOffsetX`** (`_OP2`, `_OP3`): slides the stock along the jaws without a
  re-apply.
- **The part** goes into its own component, jointed to the stock. Use the whole occurrence as
  every setup's model so "from preceding setup" stock works.
- **Setups belong to a station by name:** `Op 1…` → OP1, `Op 2…` → OP2, `Op 3…` → OP3. Put
  `[noFM]` in a setup's name to skip it.
- **Save** the design before the first Apply. Fusion only accepts referenced components in a
  saved document.

## 4. Use it

**Apply / Swap Fixture** (Design or Manufacture workspace):

| Input | Meaning |
|---|---|
| Station | **OP1** (the stack is grounded and the part goes to it) or **OP2** / **OP3** (the stack hangs off `FM_OP2` / `FM_OP3`). Each station keeps its own settings |
| Workpiece origin | `FM_Stock` (OP1) or `FM_OP2` / `FM_OP3`, prefilled when found |
| Fixture / vise | pick from the library; `(none)` removes that station's stack |
| Zero point plate, Plate position, Rotation on plate | the plate, a position with a stud pattern the fixture has, and 0 / 90 / 180 / 270° |
| Stock mount point | (single-mount fixtures) where the stock sits, normally `FM_Mount` |
| Jaws, Jaw position, Number of jaws | (vises and chucks) the jaws that fit, the bolt position, and for chucks how many jaws |
| Orientation | normal or reversed (hard jaws with `FM_GripRev`) |
| Seat part on | vise: **Jaw step** or **Top of jaw base**; chuck: **Chuck face**, **Jaw step 1** or **Jaw step 2** |
| Jaw opening | OP1 hard jaws close flush on the stock automatically (they follow `StockY`). OP2 / OP3 hard jaws and soft jaws use the width you enter |
| Start from new soft jaw blanks | replace cut soft jaws with fresh copies of the blank |
| X / Y / Z offset, Rotation, Flip | adjustment on top of the seat |
| Point setups at container | sets each setup's fixture to its station's container |
| Regenerate toolpaths | regenerates all toolpaths after the change |

What Apply builds for a station with a plate:

```
FM Fixture  (OP1)                                FM OP2  (OP2, hangs off the stock)
├── <plate>    FM_PlateMount → machine position  ├── <plate>
├── <vise>     FM_ZP_<pattern> → plate receiver  ├── <vise>
├── jaws       FM_Bolt → FM_JawSeat_*            ├── FM Soft Jaw 1 / 2 (local copies)
└── FM OP1 WCS FM_WCS_OP1 → plate FM_WCS         └── FM OP2 WCS
stock FM_Stock → jaw 1 grip                      jaw 1 grip → stock FM_OP2
```

- **WCS:** after the first Apply of a station with a plate, pick each setup's WCS from that
  station's WCS component (origin `FM_WCS_OP1` / `FM_WCS_OP2`). Swaps keep the selection.
- **Soft jaws:** cut them in their own setup (e.g. "Cut Soft Jaws", no station in its name) with
  the `FM Soft Jaw` bodies as the model. Apply also makes hidden `FM Soft Jaw Stock` bodies, the
  uncut blanks, and sets them as that setup's stock.

**Register Fixture** adds or updates the active design in the library. **Fixture Library** sets
the library path, shows each entry's details, opens a fixture's design, removes entries (the
Fusion file is not touched), and checks that every URN still resolves.

## Limits and notes

- **Don't use Undo to reverse an Apply with jaws.** It can bring old jaws back as broken
  references. Re-apply the previous settings instead.
- **After updating the add-in's files, Stop and Run it** in Scripts and Add-Ins.
- **Library files are inserted at their latest version.** If one changes later, use Get Latest.
  If that fails for a reference inside a station container, delete the occurrence and Apply
  again with the same settings.
- **The machine isn't set by the add-in.** Pick each setup's machine yourself.
- **One stack per station**, three stations per file.
- If a joint can't be solved, Apply stops with Fusion's message and rolls the whole command back.

## Layout

```
FixtureManager.py / .manifest   add-in entry point
fm/library.py                   JSON library (no Fusion dependency)
fm/jawmath.py                   naming, jaw and plate placement math (no Fusion dependency)
fm/fusion_ops.py                insert / swap / joint / station / WCS / CAM logic
fm/commands.py                  toolbar commands and dialogs
fm/updater.py                   publish to / update from GitHub via git
resources/                      icons (regenerate with tools/make_icons.py)
tests/                          python3 -m unittest discover tests
```

## License

MIT. See [LICENSE](LICENSE).
