"""Streamlit visualizer for verifying feature extraction and probe approach
trajectories before final PC-DMIS export.

Run via: python -m cmm_gen.cli visualize
(or directly: streamlit run cmm_gen/visualizer/app.py)

STATUS: layout/skeleton only. The 3D feature + approach-vector plot wires up
once cad_parser.extract_features and kinematics_validator.validate_feature
are implemented (pending the clarifying questions).
"""

from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="CMM-Gen Visualizer", layout="wide")

st.title("CMM-Gen: Feature & Approach Vector Visualizer")

with st.sidebar:
    st.header("Inputs")
    step_file = st.file_uploader("STEP file", type=["step", "stp"])
    blueprint_file = st.file_uploader("Blueprint", type=["pdf", "png", "jpg", "jpeg"])
    run = st.button("Run extraction", disabled=step_file is None)

tab_features, tab_gdt, tab_kinematics = st.tabs(
    ["CAD Features", "GD&T Mapping", "Approach Vectors"]
)

with tab_features:
    st.info(
        "Upload a STEP file and click 'Run extraction' to see extracted "
        "planes/cylinders/slots/etc. here once cad_parser.extract_features "
        "is implemented."
    )

with tab_gdt:
    st.info(
        "Blueprint callouts and their mapped CAD features will be shown "
        "here, overlaid on the blueprint image, once gdt_extractor is "
        "implemented."
    )

with tab_kinematics:
    st.info(
        "A 3D plot of the part with approach/retract/clearance vectors "
        "color-coded by validation status (valid / rejected: collision, "
        "head-angle, travel-limit, blind-spot) will render here once "
        "kinematics_validator is implemented."
    )
