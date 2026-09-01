# Drawing a chart

Read this when the planner asks for a chart, a graph or a picture of the numbers.
It is the whole recipe; the numbers themselves come from the procedure in
`SKILL.md`, and a chart about records still ends with the link to the set.

Write every chart as a **self-contained `.html` file** into
**`/mnt/session/outputs/`** (`mkdir -p` it first). That directory is what the
planner's screen renders from; a chart written anywhere else is seen by nobody.
**Self-contained means the SVG is inlined** — never reference a CDN, an external
stylesheet or a separate image: the page is opened later in another browser, so
anything it must fetch can fail or leak.

```python
import io, pathlib, matplotlib

matplotlib.use("Agg")  # no display in the sandbox
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(12, 6))
# ... plot the numbers you resolved ...
fig.tight_layout()

buf = io.StringIO()
fig.savefig(buf, format="svg")  # SVG, not PNG
out = pathlib.Path("/mnt/session/outputs/open-spare-parts-by-branch.html")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(
    "<!doctype html><meta charset=utf-8>"
    "<title>Open spare-parts cards by branch</title>"
    # matplotlib writes a FIXED-size SVG; without this the chart is cropped and
    # grows its own scrollbars inside the chat frame. Scaling on BOTH axes keeps
    # any aspect ratio fully visible.
    "<style>html,body{height:100%;margin:0}"
    "body{display:grid;place-items:center;font-family:system-ui}"
    "svg{max-width:100%;max-height:100%;width:auto;height:auto}</style>" + buf.getvalue(),
    encoding="utf-8",
)
print(f"wrote {out}")
```

The `<title>` becomes the browser tab name. **The filename is business-facing** —
the planner sees it as the caption above the chart — so name it in their words,
never with a code, an id or a field name. Then ONE line on what the chart shows.
Not the filename, not the directory, not that a file was written.
**Do not read the chart back** — it returns the whole file into the conversation to
tell you what you just plotted.

**Axis labels, legends and the title are business names**, resolved through the
phrasebook like any other output — never a code, an id or a field name. Reply in
the language the planner wrote in, chart labels included.

`matplotlib` is already installed in the sandbox, with `numpy`, `pandas` and
`PIL`. **`plotly` is NOT** — a chart written against it fails there.
