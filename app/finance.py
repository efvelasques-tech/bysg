from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
ET.register_namespace("", MAIN_NS)
ET.register_namespace("r", REL_NS)


def _sheet_path(archive: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relation_id = None
    for sheet in workbook.find(f"{{{MAIN_NS}}}sheets") or []:
        if sheet.attrib.get("name") == sheet_name:
            relation_id = sheet.attrib.get(f"{{{REL_NS}}}id")
            break
    if not relation_id:
        raise ValueError(f"No se encontró la hoja {sheet_name}.")
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for relation in relationships:
        if relation.attrib.get("Id") == relation_id:
            target = relation.attrib["Target"].lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    raise ValueError(f"No se encontró la relación de la hoja {sheet_name}.")


def _set_numeric(root: ET.Element, reference: str, value: int) -> None:
    cell = root.find(f".//{{{MAIN_NS}}}c[@r='{reference}']")
    if cell is None:
        raise ValueError(f"No se encontró la celda {reference} en la planilla financiera.")
    for child in list(cell):
        if child.tag in {f"{{{MAIN_NS}}}f", f"{{{MAIN_NS}}}v", f"{{{MAIN_NS}}}is"}:
            cell.remove(child)
    cell.attrib.pop("t", None)
    numeric = ET.SubElement(cell, f"{{{MAIN_NS}}}v")
    numeric.text = str(value / 100)


def sync_workbook(workbook_path: Path, financials: list[dict]) -> None:
    if not workbook_path.is_file():
        return
    by_code = {row["code"]: row for row in financials}
    row_map = {"REM": 8, "BUZ": 9, "GOR": 10}
    with zipfile.ZipFile(workbook_path, "r") as source:
        sheet_path = _sheet_path(source, "Productos")
        sheet_root = ET.fromstring(source.read(sheet_path))
        for code, row_number in row_map.items():
            row = by_code[code]
            for column, field in (
                ("D", "finished_product_cents"),
                ("E", "bag_cents"),
                ("F", "keychain_cents"),
                ("G", "other_cost_cents"),
                ("I", "price_cents"),
            ):
                _set_numeric(sheet_root, f"{column}{row_number}", int(row[field]))
        sheet_bytes = ET.tostring(sheet_root, encoding="utf-8", xml_declaration=True)

        workbook_root = ET.fromstring(source.read("xl/workbook.xml"))
        calc = workbook_root.find(f"{{{MAIN_NS}}}calcPr")
        if calc is None:
            calc = ET.SubElement(workbook_root, f"{{{MAIN_NS}}}calcPr")
        calc.attrib.update({"calcMode": "auto", "fullCalcOnLoad": "1", "forceFullCalc": "1"})
        workbook_bytes = ET.tostring(workbook_root, encoding="utf-8", xml_declaration=True)

        descriptor, temporary_name = tempfile.mkstemp(prefix="Finanzas-BYSG-", suffix=".xlsx", dir=workbook_path.parent)
        os.close(descriptor)
        try:
            with zipfile.ZipFile(temporary_name, "w", zipfile.ZIP_DEFLATED) as target:
                for entry in source.infolist():
                    data = source.read(entry.filename)
                    if entry.filename == sheet_path:
                        data = sheet_bytes
                    elif entry.filename == "xl/workbook.xml":
                        data = workbook_bytes
                    target.writestr(entry, data)
            os.replace(temporary_name, workbook_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
