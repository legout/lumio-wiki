"""Generate the DOCX and PDF Knowledge Sources for the Atlas Heatworks trial."""

# The source prose intentionally stays readable as paragraphs instead of line-fragment tuples.
# ruff: noqa: E501

import sys
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"


def generate_docx() -> Path:
    document_class = import_module("docx").Document
    path = SOURCES / "installation-handbook.docx"
    document = document_class()
    document.core_properties.title = "Atlas Heatworks Installation Handbook"
    document.core_properties.subject = "Aster 8 and Aster 12 installation and commissioning"
    document.core_properties.author = "Tomas Reed, Technical Training Lead"

    document.add_heading("Atlas Heatworks Installation Handbook", 0)
    document.add_paragraph("Controlled document IH-4.1 · Effective 10 February 2026")
    document.add_paragraph(
        "Applies to Aster 8 and Aster 12 installations. Fictional demonstration document."
    )

    document.add_heading("1. Responsibility and qualification", level=1)
    document.add_paragraph(
        "Only an Atlas-certified installer may commission an Aster system. Tomas Reed, Technical Training Lead, "
        "owns installer certification and this handbook. Electrical and refrigerant work must follow applicable "
        "local rules and the equipment safety labels."
    )

    document.add_heading("2. Pre-installation survey", level=1)
    for item in (
        "Complete a room-by-room heat-loss calculation; catalog nominal output is not a substitute for sizing.",
        "Confirm the number of heating zones and the required domestic-hot-water capacity.",
        "Record the outdoor-unit location, condensate route, electrical isolation point, and service access.",
        "Verify that the selected service hub can reach the installation postcode.",
        "Obtain the customer's approval of equipment position and expected noise envelope.",
    ):
        document.add_paragraph(item, style="List Bullet")

    document.add_heading("3. Model-specific clearances", level=1)
    table = document.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    header = table.rows[0].cells
    header[0].text = "Requirement"
    header[1].text = "Aster 8"
    header[2].text = "Aster 12"
    for requirement, aster8, aster12 in (
        ("Rear service clearance", "300 mm", "350 mm"),
        ("Front airflow clearance", "1,000 mm", "1,200 mm"),
        ("Minimum primary pipe diameter", "22 mm", "28 mm"),
        ("Standard cylinder", "180 litres", "250 litres"),
    ):
        cells = table.add_row().cells
        cells[0].text = requirement
        cells[1].text = aster8
        cells[2].text = aster12

    document.add_heading("4. Installation sequence", level=1)
    for step in (
        "Isolate electrical and hydraulic supplies and document the lockout.",
        "Position and level the outdoor unit; verify clearances and condensate fall.",
        "Install primary pipework, strainers, isolation valves, and the selected cylinder.",
        "Pressure-test the hydraulic circuit and record the test result before insulation.",
        "Connect the Atlas Link controller and label every heating zone.",
        "Complete electrical safety tests before energising the system.",
    ):
        document.add_paragraph(step, style="List Number")

    document.add_heading("5. Commissioning gate", level=1)
    document.add_paragraph(
        "Before commissioning, the installer must confirm correct water pressure, verified flow rate, successful "
        "electrical safety tests, unobstructed airflow, functional condensate drainage, and a completed controller "
        "configuration. A refrigerant alarm or failed safety test blocks commissioning."
    )
    document.add_paragraph(
        "The commissioning date starts the standard five-year product warranty. Registration for CarePlus must be "
        "submitted within 30 calendar days of that date."
    )

    document.add_heading("6. Handover records", level=1)
    for item in (
        "Signed commissioning record, including measured flow and electrical test results.",
        "Model and serial numbers for the outdoor unit, controller, and cylinder.",
        "Customer demonstration of normal controls and emergency shutdown.",
        "First annual preventive-maintenance due date.",
        "Photographs of installed clearances and labelled isolation points.",
    ):
        document.add_paragraph(item, style="List Bullet")

    document.add_page_break()
    document.add_heading("7. Escalation", level=1)
    document.add_paragraph(
        "Technical deviations require written approval from the Technical Training Lead before commissioning. "
        "Warranty exceptions are outside this handbook and remain the responsibility of the Operations Director "
        "under the Customer Support and Warranty Policy."
    )

    document.save(path)
    return path


def generate_pdf() -> Path:
    colors = import_module("reportlab.lib.colors")
    enums = import_module("reportlab.lib.enums")
    pagesizes = import_module("reportlab.lib.pagesizes")
    style_module = import_module("reportlab.lib.styles")
    units = import_module("reportlab.lib.units")
    platypus = import_module("reportlab.platypus")
    A4 = pagesizes.A4
    Paragraph = platypus.Paragraph
    ParagraphStyle = style_module.ParagraphStyle
    SimpleDocTemplate = platypus.SimpleDocTemplate
    Spacer = platypus.Spacer
    Table = platypus.Table
    TableStyle = platypus.TableStyle
    TA_CENTER = enums.TA_CENTER
    getSampleStyleSheet = style_module.getSampleStyleSheet
    mm = units.mm

    path = SOURCES / "2025-impact-report.pdf"
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "CenteredTitle", parent=styles["Title"], alignment=TA_CENTER, spaceAfter=10
    )
    small = ParagraphStyle(
        "Small",
        parent=styles["BodyText"],
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#555555"),
    )
    body = styles["BodyText"]
    body.leading = 14

    story = [
        Paragraph("Atlas Heatworks 2025 Impact Report", title),
        Paragraph(
            "Reporting period: 1 January–31 December 2025 · Issued 20 March 2026",
            styles["Heading3"],
        ),
        Paragraph(
            "Fictional demonstration document. Figures are internally generated estimates and are not independently audited.",
            small,
        ),
        Spacer(1, 6 * mm),
        Paragraph("Executive summary", styles["Heading1"]),
        Paragraph(
            "Atlas Heatworks commissioned 428 Aster systems during 2025 across Alder County. The installed fleet "
            "comprised 265 Aster 8 systems (62 percent) and 163 Aster 12 systems (38 percent). Using Atlas's "
            "published baseline methodology, those installations are estimated to avoid 1,840 tonnes of carbon-"
            "dioxide equivalent over their first full operating year compared with the replaced heating systems.",
            body,
        ),
        Spacer(1, 4 * mm),
        Paragraph("Key indicators", styles["Heading1"]),
    ]

    data = [
        ["Indicator", "2025 result", "Context"],
        ["Systems commissioned", "428", "265 Aster 8; 163 Aster 12"],
        ["Estimated first-year emissions avoided", "1,840 tCO2e", "Modelled, not audited"],
        ["Recovered refrigerant sent for reclamation", "96%", "By mass from service returns"],
        ["CarePlus registrations on time", "89%", "Within 30 days of commissioning"],
        ["BrightHome Access Fund installations", "37", "Income-qualified households"],
        ["Installer recertification completion", "100%", "All 24 field installers"],
    ]
    table = Table(data, colWidths=[62 * mm, 38 * mm, 72 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4d46")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#888888")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#edf4f1")]),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("LEADING", (0, 0), (-1, -1), 11),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.extend(
        [
            table,
            Spacer(1, 6 * mm),
            Paragraph("Supply chain and service operations", styles["Heading1"]),
            Paragraph(
                "Norrby Climate Systems manufactured every Aster 8 and Aster 12 outdoor unit commissioned by "
                "Atlas in 2025. Atlas retained installation, commissioning, customer support, and annual maintenance "
                "responsibility. East Yard completed 54 percent of field service visits and West Yard completed "
                "46 percent.",
                body,
            ),
            Spacer(1, 4 * mm),
            Paragraph("Community access", styles["Heading1"]),
            Paragraph(
                "The BrightHome Access Fund supported 37 of the year's 428 installations. The fund received one "
                "percent of 2025 installation revenue and was administered by the Community Programs team. Grant "
                "eligibility did not change product selection, commissioning, warranty, or CarePlus requirements.",
                body,
            ),
            Spacer(1, 4 * mm),
            Paragraph("Method and limitations", styles["Heading1"]),
            Paragraph(
                "Avoided-emissions estimates compare declared pre-installation fuel use with modelled electricity "
                "consumption under a standard weather year. They are portfolio estimates, not guarantees for an "
                "individual building. Refrigerant reclamation is calculated from contractor transfer records. "
                "The report contains no customer-level data.",
                body,
            ),
        ]
    )

    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="Atlas Heatworks 2025 Impact Report",
        author="Atlas Heatworks Community Programs Team",
    )
    document.build(story)
    return path


if __name__ == "__main__":
    SOURCES.mkdir(parents=True, exist_ok=True)
    for generated in (generate_docx(), generate_pdf()):
        sys.stdout.write(f"{generated.relative_to(ROOT)}\n")
