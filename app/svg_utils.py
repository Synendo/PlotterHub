import re

from lxml import etree
from pathlib import Path

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
NS = {"svg": SVG_NS, "inkscape": INKSCAPE_NS}

LAYER_TAG = f"{{{SVG_NS}}}g"
GROUPMODE_ATTR = f"{{{INKSCAPE_NS}}}groupmode"
LABEL_ATTR = f"{{{INKSCAPE_NS}}}label"


def parse_dim_to_mm(s: str) -> float | None:
    """Parse an SVG length attribute into millimetres. Accepts mm, cm, in, px (or unitless = px)."""
    if not s:
        return None
    m = re.match(r"^\s*([\d.eE+\-]+)\s*([a-zA-Z%]*)\s*$", s)
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "px").lower()
    if unit == "mm":
        return value
    if unit == "cm":
        return value * 10.0
    if unit == "in":
        return value * 25.4
    if unit == "px" or unit == "":
        return value * 25.4 / 96.0
    return None


def _top_level_layers(root):
    return [g for g in root if g.tag == LAYER_TAG and g.get(GROUPMODE_ATTR) == "layer"]


def parse_layers(svg_path: Path) -> dict:
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    layers = []
    for i, g in enumerate(_top_level_layers(root)):
        label = g.get(LABEL_ATTR) or f"Layer {i + 1}"
        layers.append(
            {
                "index": i,
                "label": label,
                "addressable": bool(label) and label[0].isdigit(),
            }
        )
    return {
        "layers": layers,
        "width": root.get("width", ""),
        "height": root.get("height", ""),
        "viewBox": root.get("viewBox", ""),
        "width_mm": parse_dim_to_mm(root.get("width", "")),
        "height_mm": parse_dim_to_mm(root.get("height", "")),
    }


def filter_to_layers(svg_path: Path, keep_indices: list[int], out_path: Path) -> None:
    tree = etree.parse(str(svg_path))
    root = tree.getroot()
    keep = set(keep_indices)
    for i, g in enumerate(_top_level_layers(root)):
        if i not in keep:
            g.getparent().remove(g)
    tree.write(str(out_path), xml_declaration=True, encoding="utf-8")


def transform_to_paper(
    svg_path: Path,
    out_path: Path,
    paper_w_mm: float,
    paper_h_mm: float,
    margin_top_mm: float,
    margin_right_mm: float,
    margin_bottom_mm: float,
    margin_left_mm: float,
    fit_content: bool,
    transform_scale: float = 1.0,
    transform_rotation_deg: float = 0.0,
    transform_offset_x_mm: float = 0.0,
    transform_offset_y_mm: float = 0.0,
) -> None:
    """Write a new SVG sized to the paper, with the source SVG's content wrapped in
    a <g transform="..."> that centers it within the margin box, optionally scales
    it to fit, and applies the user's scale/rotation/offset around the content center.

    The output SVG uses mm as its user-unit coordinate space (viewBox = 0 0 paper_w paper_h)
    so pyaxidraw renders it 1:1 on the plotter bed.
    """
    tree = etree.parse(str(svg_path))
    root = tree.getroot()

    orig_w_mm = parse_dim_to_mm(root.get("width", "")) or paper_w_mm
    orig_h_mm = parse_dim_to_mm(root.get("height", "")) or paper_h_mm

    vb = root.get("viewBox", "")
    if vb:
        parts = vb.split()
        vb_x, vb_y, vb_w, vb_h = (float(p) for p in parts[:4])
    else:
        vb_x, vb_y = 0.0, 0.0
        vb_w, vb_h = orig_w_mm, orig_h_mm

    available_w = max(0.0, paper_w_mm - margin_left_mm - margin_right_mm)
    available_h = max(0.0, paper_h_mm - margin_top_mm - margin_bottom_mm)

    if fit_content and orig_w_mm > 0 and orig_h_mm > 0 and available_w > 0 and available_h > 0:
        fit_scale = min(available_w / orig_w_mm, available_h / orig_h_mm)
    else:
        fit_scale = 1.0

    total_mm_scale = fit_scale * transform_scale
    # Source user units -> paper mm.
    user_scale = total_mm_scale * (orig_w_mm / vb_w) if vb_w else total_mm_scale

    # Rotate/scale the content around its own center; that center lands at
    # (center_x_mm, center_y_mm) on the paper (the middle of the available area,
    # shifted by the user's offset).
    center_x_mm = margin_left_mm + available_w / 2 + transform_offset_x_mm
    center_y_mm = margin_top_mm + available_h / 2 + transform_offset_y_mm
    vb_center_x = vb_x + vb_w / 2
    vb_center_y = vb_y + vb_h / 2

    nsmap = {k: v for k, v in root.nsmap.items() if k}
    nsmap[None] = SVG_NS
    new_root = etree.Element(f"{{{SVG_NS}}}svg", nsmap=nsmap)
    new_root.set("width", f"{paper_w_mm}mm")
    new_root.set("height", f"{paper_h_mm}mm")
    new_root.set("viewBox", f"0 0 {paper_w_mm} {paper_h_mm}")

    group = etree.SubElement(new_root, f"{{{SVG_NS}}}g")
    group.set(
        "transform",
        f"translate({center_x_mm},{center_y_mm}) "
        f"rotate({transform_rotation_deg}) "
        f"scale({user_scale}) "
        f"translate({-vb_center_x},{-vb_center_y})",
    )
    for child in list(root):
        group.append(child)

    etree.ElementTree(new_root).write(str(out_path), xml_declaration=True, encoding="utf-8")
