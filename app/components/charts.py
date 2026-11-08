"""Plotly chart helpers following the project's data-viz rules: one hue for a
single series (no legend), sequential blue ramp for magnitude, fixed
categorical order for identity, thin rounded bars, recessive grid, tooltips."""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

SERIES_1 = "#2a78d6"
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SEQUENTIAL = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
TIER_COLORS = {"core": CATEGORICAL[0], "extended": CATEGORICAL[1]}


def _style(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(height=height, margin=dict(l=8, r=16, t=40, b=8),
                      bargap=0.35, showlegend=fig.layout.showlegend,
                      hoverlabel=dict(namelength=-1))
    fig.update_xaxes(showgrid=True, gridwidth=1, zeroline=True)
    fig.update_yaxes(showgrid=False)
    return fig


def hbar(df: pd.DataFrame, value: str, label: str, title: str, n: int = 10,
         value_title: str = "FY2026 obligations (USD)", hover: list[str] | None = None,
         height: int | None = None) -> go.Figure:
    d = df.head(n).iloc[::-1].copy()
    d[label] = d[label].astype(str).str.slice(0, 60)
    fig = px.bar(d, x=value, y=label, orientation="h", title=title,
                 hover_data=hover or [], color_discrete_sequence=[SERIES_1])
    fig.update_traces(marker_cornerradius=4, hovertemplate=None)
    fig.update_xaxes(title=value_title, tickprefix="$" if "USD" in value_title else "")
    fig.update_yaxes(title=None)
    return _style(fig, height or max(260, 34 * len(d) + 80))


def vbar(df: pd.DataFrame, x: str, y: str, title: str, y_title: str) -> go.Figure:
    fig = px.bar(df, x=x, y=y, title=title, color_discrete_sequence=[SERIES_1])
    fig.update_traces(marker_cornerradius=4)
    fig.update_yaxes(title=y_title, showgrid=True)
    fig.update_xaxes(title=None, showgrid=False)
    return _style(fig, 320)


def state_map(df: pd.DataFrame, state_col: str, value: str, title: str) -> go.Figure:
    fig = px.choropleth(df, locations=state_col, locationmode="USA-states", color=value,
                        scope="usa", title=title, color_continuous_scale=SEQUENTIAL,
                        labels={value: "FY2026 obligations (USD)"})
    fig.update_layout(height=380, margin=dict(l=0, r=0, t=40, b=0))
    return fig


def heatmap(mat: pd.DataFrame, title: str) -> go.Figure:
    fig = px.imshow(mat.astype(float), text_auto=".0f", aspect="auto", title=title,
                    color_continuous_scale=SEQUENTIAL, zmin=0, zmax=100,
                    labels=dict(color="Overlap %"))
    fig.update_layout(height=max(320, 40 * len(mat) + 120), margin=dict(l=8, r=8, t=40, b=8))
    return fig
