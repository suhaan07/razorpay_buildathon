"""Minimal, inline-styled HTML wrapper for escalation emails. Email clients
strip <style> blocks and external CSS unreliably, so every rule here is
inline — deliberately plain, not a design system, just enough structure
(heading, body, an invoice table, one or two CTA buttons) to read as a real
notification instead of a wall of plain text."""

from __future__ import annotations

_ACCENT = "#4338ca"
_MUTED = "#676c78"
_BORDER = "#e3e5ea"
_HIGHLIGHT = "#fff7e6"


def build_escalation_html(
    *,
    heading: str,
    intro_text: str,
    highlighted_invoice_no: str,
    invoice_rows: list[dict],
    primary_label: str | None = None,
    primary_url: str | None = None,
    secondary_label: str | None = None,
    secondary_url: str | None = None,
) -> str:
    """Both buttons are optional — omitted entirely (not rendered as a dead
    link) when the caller has no real payment link to offer, e.g. an
    amount over Razorpay's current per-link maximum (see
    razorpay_client.is_oversized_stub). The explanation for why belongs in
    intro_text; this function never fabricates one."""

    intro_html = "".join(f"<p style='margin:0 0 10px'>{line}</p>" for line in intro_text.split("\n") if line.strip())

    rows_html = "".join(
        f"""<tr style="background:{_HIGHLIGHT if row['invoice_no'] == highlighted_invoice_no else '#ffffff'}">
            <td style="padding:8px 12px;border-bottom:1px solid {_BORDER};font-family:monospace;font-size:13px">{row['invoice_no']}</td>
            <td style="padding:8px 12px;border-bottom:1px solid {_BORDER};font-size:13px">{row['due_date_display']}</td>
            <td style="padding:8px 12px;border-bottom:1px solid {_BORDER};font-size:13px;text-align:right">{row['amount_display']}</td>
        </tr>"""
        for row in invoice_rows
    )

    primary_button = ""
    if primary_label and primary_url:
        primary_button = (
            f'<a href="{primary_url}" style="display:inline-block;padding:11px 22px;border-radius:6px;'
            f'background:{_ACCENT};color:#fff;text-decoration:none;font-weight:600;font-size:14px">{primary_label}</a>'
        )

    secondary_button = ""
    if secondary_label and secondary_url:
        margin = "margin-left:10px;" if primary_button else ""
        secondary_button = (
            f'<a href="{secondary_url}" style="display:inline-block;{margin}padding:11px 22px;'
            f'border-radius:6px;border:1px solid {_ACCENT};color:{_ACCENT};text-decoration:none;'
            f'font-weight:600;font-size:14px">{secondary_label}</a>'
        )

    buttons_html = (
        f'  <div>\n    {primary_button}\n    {secondary_button}\n  </div>\n'
        if (primary_button or secondary_button) else ""
    )

    return f"""
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#14161c;max-width:560px;margin:0 auto">
  <div style="border-left:4px solid {_ACCENT};padding:2px 0 2px 14px;margin-bottom:18px">
    <div style="font-size:18px;font-weight:700">{heading}</div>
  </div>
  <div style="font-size:14px;line-height:1.6;margin-bottom:18px">{intro_html}</div>
  <table style="width:100%;border-collapse:collapse;margin-bottom:22px">
    <thead>
      <tr style="background:#f3f4f7">
        <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:{_MUTED}">Invoice</th>
        <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:{_MUTED}">Due date</th>
        <th style="padding:8px 12px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:{_MUTED}">Amount</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
{buttons_html}  <div style="margin-top:26px;font-size:11px;color:#9aa0ad">Automated reminder from the AI Revenue Recovery system.</div>
</div>
""".strip()
