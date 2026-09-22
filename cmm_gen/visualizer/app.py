"""Streamlit visualizer for verifying feature extraction and probe approach
trajectories before final PC-DMIS export.

Run via: python -m cmm_gen.cli visualize
(or directly: streamlit run cmm_gen/visualizer/app.py)

This is a local, file-path-driven tool (point it at a STEP file already on
disk) rather than a hosted upload flow, since it's meant to run on a
programmer's own machine next to the CAD files they're already working with.
"""

from __future__ import annotations

from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from cmm_gen import cad_parser, kinematics_validator
from cmm_gen.models import ApproachVector, ApproachVectorStatus, CADFeature

st.set_page_config(page_title="CMM-Gen Visualizer", layout="wide")
st.title("CMM-Gen: Feature & Approach Vector Visualizer")

_FEATURE_COLORS = {
    "PLANE": "#4C78A8",
    "CYLINDER": "#F58518",
    "CONE": "#54A24B",
    "SLOT": "#E45756",
    "CIRCLE": "#B279A2",
    "SPHERE": "#9D755D",
}


@st.cache_data
def _load_probe_names() -> list[str]:
    import yaml

    if not kinematics_validator.DEFAULT_PROBE_LIBRARY_PATH.exists():
        return []
    data = yaml.safe_load(kinematics_validator.DEFAULT_PROBE_LIBRARY_PATH.read_text())
    return sorted(data.get("probes", {}))


@st.cache_data
def _load_machine_names() -> list[str]:
    import yaml

    if not kinematics_validator.DEFAULT_MACHINE_ENVELOPE_PATH.exists():
        return []
    data = yaml.safe_load(kinematics_validator.DEFAULT_MACHINE_ENVELOPE_PATH.read_text())
    return sorted(data.get("machines", {}))


def _features_figure(features: list[CADFeature]) -> go.Figure:
    fig = go.Figure()
    by_type: dict[str, list[CADFeature]] = {}
    for f in features:
        by_type.setdefault(f.type.value, []).append(f)

    for type_name, feats in by_type.items():
        fig.add_trace(
            go.Scatter3d(
                x=[f.nominal_location.x for f in feats],
                y=[f.nominal_location.y for f in feats],
                z=[f.nominal_location.z for f in feats],
                mode="markers",
                name=type_name,
                marker={"size": 5, "color": _FEATURE_COLORS.get(type_name, "#888888")},
                text=[f.id for f in feats],
                hovertemplate="%{text}<br>(%{x:.2f}, %{y:.2f}, %{z:.2f})<extra></extra>",
            )
        )
    fig.update_layout(
        scene={"aspectmode": "data"},
        margin={"l": 0, "r": 0, "t": 30, "b": 0},
        legend_title_text="Feature type",
    )
    return fig


def _approach_vectors_figure(vectors: list[ApproachVector]) -> go.Figure:
    fig = go.Figure()
    status_colors = {
        ApproachVectorStatus.VALID: "#2CA02C",
        ApproachVectorStatus.REJECTED_COLLISION: "#D62728",
        ApproachVectorStatus.REJECTED_HEAD_ANGLE: "#FF7F0E",
        ApproachVectorStatus.REJECTED_TRAVEL_LIMIT: "#9467BD",
        ApproachVectorStatus.REJECTED_BLIND_SPOT: "#8C564B",
    }
    for status, color in status_colors.items():
        subset = [v for v in vectors if v.status == status]
        if not subset:
            continue
        xs, ys, zs = [], [], []
        for v in subset:
            xs += [v.clearance_point.x, v.contact_point.x, None]
            ys += [v.clearance_point.y, v.contact_point.y, None]
            zs += [v.clearance_point.z, v.contact_point.z, None]
        fig.add_trace(
            go.Scatter3d(
                x=xs,
                y=ys,
                z=zs,
                mode="lines+markers",
                name=status.value,
                line={"color": color, "width": 4},
                marker={"size": 3, "color": color},
                text=[v.feature_id for v in subset for _ in range(3)],
                hovertemplate="%{text}<extra></extra>",
            )
        )
    fig.update_layout(
        scene={"aspectmode": "data"},
        margin={"l": 0, "r": 0, "t": 30, "b": 0},
        legend_title_text="Approach status",
    )
    return fig


with st.sidebar:
    st.header("Inputs")
    _fixtures_dir = Path(__file__).parent.parent.parent / "tests" / "fixtures"
    default_step = str(_fixtures_dir / "sample_part.step")
    step_path_str = st.text_input("STEP file path", value=default_step)

    probe_names = _load_probe_names()
    probe_name = st.selectbox("Probe", probe_names) if probe_names else None

    machine_names = _load_machine_names()
    machine_name = st.selectbox("Machine envelope", machine_names) if machine_names else None

    run = st.button("Run extraction", disabled=not step_path_str)

tab_features, tab_kinematics, tab_gdt = st.tabs(
    ["CAD Features", "Approach Vectors", "GD&T Mapping"]
)

if run:
    step_path = Path(step_path_str)
    if not step_path.exists():
        st.session_state["error"] = f"STEP file not found: {step_path}"
    else:
        try:
            shape = cad_parser.load_step_file(step_path)
            features = cad_parser.extract_features(shape)
            part_bbox = cad_parser.compute_bounding_box(shape)
            st.session_state["features"] = features
            st.session_state["part_bbox"] = part_bbox
            st.session_state["error"] = None

            if probe_name and machine_name:
                probe = kinematics_validator.load_probe_config(probe_name)
                envelope = kinematics_validator.load_machine_envelope(machine_name)
                vectors = [
                    kinematics_validator.validate_feature(f, probe, envelope, part_bbox)
                    for f in features
                ]
                st.session_state["vectors"] = vectors
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            st.session_state["error"] = str(exc)

if st.session_state.get("error"):
    st.error(st.session_state["error"])

with tab_features:
    stored_features = st.session_state.get("features")
    if not stored_features:
        st.info("Enter a STEP file path in the sidebar and click 'Run extraction'.")
    else:
        st.plotly_chart(_features_figure(stored_features), width="stretch")
        st.dataframe(
            [
                {
                    "id": f.id,
                    "type": f.type.value,
                    "x": f.nominal_location.x,
                    "y": f.nominal_location.y,
                    "z": f.nominal_location.z,
                    "diameter": f.diameter,
                }
                for f in stored_features
            ],
            width="stretch",
        )

with tab_kinematics:
    stored_vectors = st.session_state.get("vectors")
    if not stored_vectors:
        st.info(
            "Select a probe and machine envelope in the sidebar and click "
            "'Run extraction' to see approach-vector validation results here."
        )
    else:
        n_valid = sum(1 for v in stored_vectors if v.status == ApproachVectorStatus.VALID)
        st.metric("Reachable features", f"{n_valid} / {len(stored_vectors)}")
        st.plotly_chart(_approach_vectors_figure(stored_vectors), width="stretch")
        st.dataframe(
            [
                {
                    "feature": v.feature_id,
                    "status": v.status.value,
                    "reason": v.rejection_reason or "",
                }
                for v in stored_vectors
                if v.status != ApproachVectorStatus.VALID
            ],
            width="stretch",
        )

with tab_gdt:
    st.info(
        "GD&T extraction calls the Claude Vision API (requires ANTHROPIC_API_KEY) "
        "and isn't wired into this live view yet -- use "
        "`python -m cmm_gen.cli extract-gdt --step ... --blueprint ...` to preview "
        "extracted/mapped callouts from the CLI in the meantime."
    )
