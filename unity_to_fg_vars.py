#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unity finished FortiGate config (.conf exported in 'fortios-yml-ish' format) -> FG_vars.yml
What it does:
- Takes FG_vars.yml as template
- Replaces VLAN IDs + LAN IP/mask (and DHCP ranges) with those found in Unity config
- Replaces WAN VLAN IDs + (static) WAN IP/mask with those found in Unity config
- Keeps: WAN names (as in FG_vars), SD-WAN members, SLA servers, remote_access blocks, etc.

Usage:
  pip install pyyaml
  python unity_to_fg_vars.py --vars FG_vars.yml --unity FG-Unity-xxx.conf --out FG_vars_from_unity.yml
"""

import argparse
import csv
import re
import ipaddress
from copy import deepcopy
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import yaml


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def split_ip_mask(ip_field: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not ip_field:
        return None, None
    parts = str(ip_field).split()
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def extract_top_level_section_lines(conf_text: str, section_name: str) -> list[str]:
    lines = conf_text.splitlines()
    start = None
    for i, l in enumerate(lines):
        if l.strip() == f"{section_name}:":
            start = i
            break
    if start is None:
        raise ValueError(f"Section '{section_name}:' not found in Unity conf.")

    end = None
    for i in range(start + 1, len(lines)):
        if re.match(r"^[A-Za-z0-9_]+:\s*$", lines[i]):
            end = i
            break

    return lines[start:end] if end else lines[start:]


def parse_system_interface_section(conf_text: str) -> Dict[str, Dict[str, Any]]:
    """
    Parses the 'system_interface:' section (the Unity conf format used here).
    Important detail:
      - Interface entries start with exactly 4 spaces: '    - NAME:'
      - Nested tagging blocks also use '-' but are more indented; we ignore them.
    """
    section = extract_top_level_section_lines(conf_text, "system_interface")

    interfaces: Dict[str, Dict[str, Any]] = {}
    current: Optional[Dict[str, Any]] = None

    for raw in section:
        m = re.match(r"^ {4}-\s+([^:]+):\s*$", raw)
        if m:
            name = m.group(1).strip().strip('"')
            current = {"name": name}
            interfaces[name] = current
            continue

        if current is None:
            continue

        # Capture nested tagging tags (e.g. UnityLAN/UnityWAN/UnityTech*)
        m_tags = re.match(r'^\s+tags:\s*(.+)$', raw)
        if m_tags:
            tval = m_tags.group(1).strip()
            tags = re.findall(r'"([^"]+)"', tval)
            if not tags:
                tags = [x for x in re.split(r'[\s,]+', tval) if x]
            current.setdefault("tags", [])
            for t in tags:
                if t not in current["tags"]:
                    current["tags"].append(t)

        # Only direct properties are at exactly 8 spaces indentation
        m = re.match(r"^ {8}([A-Za-z0-9_\-]+):\s*(.*)$", raw)
        if not m:
            continue

        key = m.group(1)
        val = m.group(2).strip()
        if not val:
            continue

        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]

        current[key] = val

    return interfaces


def build_unity_interface_maps(conf_text: str) -> tuple[
    Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[int, list[str]], Dict[str, list[str]]
]:
    raw = parse_system_interface_section(conf_text)

    parsed: Dict[str, Dict[str, Any]] = {}
    for name, info in raw.items():
        ip, mask = split_ip_mask(info.get("ip"))
        vlanid = int(info["vlanid"]) if "vlanid" in info and str(info["vlanid"]).isdigit() else None

        parsed[name] = {
            "ip": ip,
            "mask": mask,
            "vlanid": vlanid,
            "parent": info.get("interface"),
            "role": info.get("role"),
            "alias": info.get("alias"),
            "tags": info.get("tags", []),
        }

    unity_lans = {n: v for n, v in parsed.items() if v.get("role") == "lan"}
    unity_wans = {n: v for n, v in parsed.items() if v.get("role") == "wan"}

    by_vid: Dict[int, list[str]] = {}
    by_ip: Dict[str, list[str]] = {}

    for n, v in parsed.items():
        if v.get("vlanid") is not None:
            by_vid.setdefault(v["vlanid"], []).append(n)
        if v.get("ip"):
            by_ip.setdefault(v["ip"], []).append(n)

    return parsed, unity_lans, unity_wans, by_vid, by_ip




# -----------------------------
# SD-WAN members (system_sdwan -> members) gateway parsing
# -----------------------------
def extract_nested_section_lines(conf_text: str, parent_section: str, child_section: str) -> list[str]:
    """
    Extract a nested section by indentation without YAML parsing.
    Example: parent_section='system_sdwan', child_section='members'
    """
    lines = conf_text.splitlines()

    # locate parent at top-level
    p_start = None
    for i, l in enumerate(lines):
        if l.strip() == f"{parent_section}:":
            p_start = i
            break
    if p_start is None:
        raise ValueError(f"Section '{parent_section}:' not found in Unity conf.")

    # capture parent block until next top-level key
    parent_block = []
    for j in range(p_start, len(lines)):
        l = lines[j]
        if j > p_start and re.match(r"^[A-Za-z0-9_]+:\s*$", l):
            break
        parent_block.append(l)

    # locate child within parent block
    c_start = None
    for i, l in enumerate(parent_block):
        if l.strip() == f"{child_section}:":
            c_start = i
            break
    if c_start is None:
        raise ValueError(f"Section '{child_section}:' not found inside '{parent_section}:'")

    # capture child block until indent decreases back to child's indent
    child_indent = len(parent_block[c_start]) - len(parent_block[c_start].lstrip())
    child_block = []
    for j in range(c_start, len(parent_block)):
        l = parent_block[j]
        if j > c_start and l.strip():
            indent = len(l) - len(l.lstrip())
            if indent <= child_indent:
                break
        child_block.append(l)

    return child_block


def parse_sdwan_gateway_map(conf_text: str) -> Dict[str, str]:
    """
    Parse system_sdwan -> members blocks and return a mapping:
      norm(interface_name) -> gateway_ip

    members entries look like:
      members:
          - 5:
              interface: "VSAT"
              gateway: 10.253.26.1
              ...
    """
    members_lines = extract_nested_section_lines(conf_text, "system_sdwan", "members")

    gw_map: Dict[str, str] = {}
    current_iface: Optional[str] = None

    for raw in members_lines:
        line = raw.rstrip()

        if re.match(r"^\s*-\s+\d+:\s*$", line):
            current_iface = None
            continue

        m_if = re.match(r'^\s*interface:\s*"?([^"]+)"?\s*$', line)
        if m_if:
            current_iface = m_if.group(1).strip()
            continue

        m_gw = re.match(r"^\s*gateway:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\s*$", line)
        if m_gw and current_iface:
            gw_map[norm(current_iface)] = m_gw.group(1)
            continue

    return gw_map


def wan_short_name(name_str: str) -> str:
    """
    Best-effort WAN short-name (often used in SD-WAN member 'interface' field).
    Examples:
      VSET_WAN_VSAT -> VSAT
      WAN_STARLINK  -> STARLINK
    """
    s = (name_str or "").strip()
    m = re.match(r"^(?:VSET_)?WAN_(.+)$", s, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    if "_" in s:
        return s.split("_")[-1]
    return s

def parse_system_dhcp_server_ranges(unity_text: str) -> dict:
    """
    Parses top-level section `system_dhcp_server` (Unity fortios-yml-ish) and returns:
        { norm(interface_name): "start-ip - end-ip" }

    If multiple ip-range entries exist per interface, we keep the first one found.
    """
    lines = extract_top_level_section_lines(unity_text, "system_dhcp_server")

    entry_start_re = re.compile(r"^ {4}-\s+\d+:\s*$")
    iface_re = re.compile(r'^ {8}interface:\s*"?([^"]+)"?\s*$')

    ip_range_header_re = re.compile(r"^ {8}ip-range:\s*$")
    range_item_re = re.compile(r"^ {12}-\s+\d+:\s*$")
    start_ip_re = re.compile(r"^ {16}start-ip:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\s*$")
    end_ip_re = re.compile(r"^ {16}end-ip:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\s*$")

    out: dict = {}
    current_iface = None
    in_ip_range = False
    current_start = None
    current_end = None

    def flush_range():
        nonlocal current_start, current_end
        if current_iface and current_start and current_end:
            key = norm(current_iface)
            # keep first range
            if key not in out:
                out[key] = f"{current_start} - {current_end}"
        current_start = None
        current_end = None

    for raw in lines:
        ln = raw.rstrip()

        if entry_start_re.match(ln):
            flush_range()
            current_iface = None
            in_ip_range = False
            continue

        m_if = iface_re.match(ln)
        if m_if:
            current_iface = m_if.group(1).strip()
            continue

        if ip_range_header_re.match(ln):
            in_ip_range = True
            continue

        if in_ip_range and range_item_re.match(ln):
            flush_range()
            continue

        if in_ip_range:
            m_s = start_ip_re.match(ln)
            if m_s:
                current_start = m_s.group(1)
                continue
            m_e = end_ip_re.match(ln)
            if m_e:
                current_end = m_e.group(1)
                continue

    flush_range()
    return out

def export_unity_interfaces_csv(unity_text: str, out_csv: Path) -> None:
    """
    Export UnityLAN/UnityWAN interfaces (role=lan/wan) to CSV:
      nombre, tag, vlanid, ip, mask, gateway, vpn

    - Excludes 'Offline' and VPN_* interfaces as rows.
    - vpn='yes' on WAN row if VPN_<WANNAME> exists.
    - gateway from system_sdwan->members, matching members.interface to WAN name;
      if exact match fails, tries wan_short_name() fallback.
    """
    parsed, unity_lans, unity_wans, by_vid, by_ip = build_unity_interface_maps(unity_text)

    # VPN suffix set: VPN_<X> => X
    vpn_suffixes = set()
    for n in parsed.keys():
        if n.upper().startswith("VPN_"):
            vpn_suffixes.add(norm(n.split("_", 1)[1]))

    gw_map = {}
    try:
        gw_map = parse_sdwan_gateway_map(unity_text)
    except Exception:
        gw_map = {}

    try:
        ippool_by_wan = parse_firewall_ippool_map(unity_text)
    except ValueError:
        ippool_by_wan = {}
    dhcp_by_iface = parse_system_dhcp_server_ranges(unity_text)

    rows = []
    for name, info in parsed.items():
        if not isinstance(name, str):
            continue
        if name.strip().upper() == "OFFLINE":
            continue
        if name.upper().startswith("VPN_"):
            continue

        # Fine-tuning exclusions
        lname = name.strip().lower()

        # 3) Do not read the mgmt_interface
        if lname == "mgmt_interface":
            continue

        # 2) Do not read bonding_wanx (bonding_wan1, bonding_wan2, bonding_wanX, ...)
        if lname.startswith("bonding_wan"):
            continue
        if name.strip().upper() == "OFFLINE":
            continue
        if name.upper().startswith("VPN_"):
            continue
        if lname.startswith("uc_"):
            continue
        if lname.startswith("transit2vrf"):
            continue

        role = (info.get("role") or "").lower()
        if role not in ("lan", "wan"):
            continue

        # 1) Do not read LANs that end with _vlan
        if role == "lan" and lname.endswith("_vlan"):
            continue
            
        is_transitwan = lname.endswith("transit") and lname != "fusion_transit"

        is_wan = role == "wan"
        tag = "TransitWAN" if is_transitwan else ("WAN" if is_wan else "LAN")
        #tag = "WAN" if is_wan else "LAN"

        ip = info.get("ip") or ""
        mask = info.get("mask") or ""
        vlanid = "" if info.get("vlanid") is None else str(info.get("vlanid"))

        vpn_flag = "no"
        if is_wan and norm(name) in vpn_suffixes:
            vpn_flag = "yes"

        gateway_val = ""
        if is_wan and gw_map:
            gateway_val = gw_map.get(norm(name), "")
            if not gateway_val:
                gateway_val = gw_map.get(norm(wan_short_name(name)), "")

        dhcp_range_val = ""
        if not is_wan and dhcp_by_iface:
            dhcp_range_val = dhcp_by_iface.get(norm(name), "")
            if not dhcp_range_val:
                dhcp_range_val = dhcp_by_iface.get(norm(wan_short_name(name)), "")

        ip_nat_pool_val = ""

        if is_wan:
           # 1) intenta con nombre exacto
           ip_nat_pool_val = ippool_by_wan.get(name, "")
           # 2) fallback con nombre corto (VSAT)
           if not ip_nat_pool_val:
                ip_nat_pool_val = ippool_by_wan.get(wan_short_name(name), "")

        tech_val = ""
        if is_wan:
            for t in (info.get("tags") or []):
                if isinstance(t, str) and t.startswith("UnityTech"):
                    tech_val = t[len("UnityTech"):]
                    break

        rows.append({
            "nombre": name,
            "tag": tag,
            "Tech": tech_val,
            "vlanid": vlanid,
            "ip": ip,
            "mask": mask,
            "gateway": gateway_val,
            "dhcp range": dhcp_range_val,
            "vpn": vpn_flag,
            "ip nat pool": ip_nat_pool_val,
        })

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["nombre", "tag", "Tech", "vlanid", "ip", "mask", "gateway", "dhcp range", "vpn", "ip nat pool","scenario"])
        writer.writeheader()
        try:
            transit_rows = parse_firewall_address_transit_rows(unity_text,  fieldnames=["nombre", "tag", "Tech", "vlanid", "ip", "mask", "gateway", "dhcp range", "vpn", "ip nat pool"])
        except ValueError:
            transit_rows = []   
        rows.extend(transit_rows)
        scenario = detect_scenario_from_rows(rows)
        for r in rows:
            r["scenario"] = scenario
        writer.writerows(rows)
        

    print(f"OK -> {out_csv} ({len(rows)} filas)")


def update_dhcp_range(dhcp_dict: Dict[str, Any], new_gw_ip: str, new_mask: str) -> None:
    """
    Keeps the last-octet ranges from the template, but moves them into the Unity subnet.
    Works best for /24 (common in our configs).
    """
    if not isinstance(dhcp_dict, dict) or not dhcp_dict.get("enable"):
        return

    ip_start = dhcp_dict.get("ip_start")
    ip_stop = dhcp_dict.get("ip_stop")
    if not (ip_start and ip_stop and new_gw_ip and new_mask):
        return

    try:
        prefix = ipaddress.IPv4Network(f"0.0.0.0/{new_mask}").prefixlen
        net = ipaddress.IPv4Interface(f"{new_gw_ip}/{prefix}").network

        s_oct = int(str(ipaddress.IPv4Address(ip_start)).split(".")[-1])
        e_oct = int(str(ipaddress.IPv4Address(ip_stop)).split(".")[-1])

        base = str(net.network_address).split(".")
        dhcp_dict["ip_start"] = ".".join(base[:3] + [str(s_oct)])
        dhcp_dict["ip_stop"] = ".".join(base[:3] + [str(e_oct)])
    except Exception:
        # leave as-is if anything is weird
        return


def map_vars_lan_to_unity(lan_item: Dict[str, Any], unity_lans: Dict[str, Dict[str, Any]], by_vid: Dict[int, list[str]]) -> Optional[str]:
    name = str(lan_item.get("name", "")).strip()
    vid = lan_item.get("vid")
    nname = norm(name)

    for u in unity_lans:
        if norm(u) == nname:
            return u

    if isinstance(vid, int) and vid in by_vid:
        candidates = [c for c in by_vid[vid] if c in unity_lans]
        if candidates:
            return candidates[0]

    # try alias match
    for u, v in unity_lans.items():
        if v.get("alias") and norm(v["alias"]) == nname:
            return u

    return None


def map_vars_wan_to_unity(
    wan_item: Dict[str, Any],
    unity_wans: Dict[str, Dict[str, Any]],
    by_vid: Dict[int, list[str]],
    by_ip: Dict[str, list[str]],
) -> Optional[str]:
    name = str(wan_item.get("name", "")).strip()
    vid = wan_item.get("vid")
    ip = wan_item.get("ip")
    nname = norm(name)

    # 1) exact name match
    for u in unity_wans:
        if norm(u) == nname:
            return u

    # 2) vlanid match
    if isinstance(vid, int) and vid in by_vid:
        candidates = [c for c in by_vid[vid] if c in unity_wans]
        if candidates:
            return candidates[0]

    # 3) ip match (static only)
    if isinstance(ip, str) and ip not in ("dhcp", "", None) and ip in by_ip:
        candidates = [c for c in by_ip[ip] if c in unity_wans]
        if candidates:
            return candidates[0]

    # 4) heuristics (only if the Unity interface exists)
    m = re.match(r"^starl(\d+)$", nname)
    if m:
        cand = f"Starlink_{int(m.group(1))}"
        return cand if cand in unity_wans else None

    if nname.startswith(("vsat", "kvh")):
        return "KVH" if "KVH" in unity_wans else None

    if nname.startswith("wifi"):
        return "WAN_eHub" if "WAN_eHub" in unity_wans else None

    if nname.startswith(("cell1", "5g1")):
        return "5G1_br1" if "5G1_br1" in unity_wans else None

    if nname.startswith(("cell2", "4g2")):
        return "4g2_hd4" if "4g2_hd4" in unity_wans else None

    if nname.startswith("shore") and isinstance(vid, int) and vid == 101:
        return "Shore_hardwired" if "Shore_hardwired" in unity_wans else None

    if nname.startswith("shore") and isinstance(vid, int) and vid == 100:
        return "Shore3_pepwave" if "Shore3_pepwave" in unity_wans else None

    if nname.startswith("bond"):
        return "Bonding_1" if "Bonding_1" in unity_wans else None

    return None


def apply_updates(vars_data: Dict[str, Any], unity_conf_text: str) -> Dict[str, Any]:
    parsed, unity_lans, unity_wans, by_vid, by_ip = build_unity_interface_maps(unity_conf_text)
    out = deepcopy(vars_data)

    # LANs
    for lan in out.get("lans", []) or []:
        if not isinstance(lan, dict):
            continue
        u_name = map_vars_lan_to_unity(lan, unity_lans, by_vid)
        if not u_name:
            continue
        u = parsed[u_name]

        if u.get("vlanid") is not None:
            lan["vid"] = u["vlanid"]
        if u.get("ip") and u.get("mask"):
            lan["ip"] = u["ip"]
            lan["mask"] = u["mask"]
            update_dhcp_range(lan.get("dhcp", {}), u["ip"], u["mask"])

    # WANs (selected)
    for wan in out.get("wans", []) or []:
        if not isinstance(wan, dict):
            continue
        u_name = map_vars_wan_to_unity(wan, unity_wans, by_vid, by_ip)
        if not u_name:
            continue
        u = parsed[u_name]

        if u.get("vlanid") is not None:
            wan["vid"] = u["vlanid"]

        # only overwrite static IPs; keep 'dhcp' as-is
        if u.get("ip") and u.get("mask") and isinstance(wan.get("ip"), str) and wan["ip"] != "dhcp":
            wan["ip"] = u["ip"]
            wan["mask"] = u["mask"]

    # WANs (standard_wans template list)
    for wan in out.get("standard_wans", []) or []:
        if not isinstance(wan, dict):
            continue
        u_name = map_vars_wan_to_unity(wan, unity_wans, by_vid, by_ip)
        if not u_name:
            continue
        u = parsed[u_name]

        if u.get("vlanid") is not None:
            wan["vid"] = u["vlanid"]

        if u.get("ip") and u.get("mask") and isinstance(wan.get("ip"), str) and wan["ip"] != "dhcp":
            wan["ip"] = u["ip"]
            wan["mask"] = u["mask"]

    return out

def parse_firewall_ippool_map(conf_text: str) -> dict:
    """
    Parses firewall_ippool section and maps pools to a WAN name (like VSAT).
    Returns: { "VSAT": "startip-endip" or "startip" }
    """
    lines = extract_top_level_section_lines(conf_text, "firewall_ippool")

    # Captura: - <POOLNAME>:
    pool_start_re = re.compile(r"^ {4}-\s+([^:]+):\s*$")
    startip_re = re.compile(r"^ {8}startip:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\s*$")
    endip_re = re.compile(r"^ {8}endip:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\s*$")

    def pool_to_wan(pool_name: str) -> str:
        """
        Heurística simple:
          VSAT_nat_pool -> VSAT
          STARLINK_pool -> STARLINK
        """
        p = pool_name.strip().strip('"')
        p = re.sub(r"(?i)_?nat_?pool$", "", p)  # quita nat_pool / NATPOOL etc.
        p = re.sub(r"(?i)_?pool$", "", p)      # quita pool
        return p

    out: dict = {}
    current_pool = None
    current_start = None
    current_end = None

    def flush():
        nonlocal current_pool, current_start, current_end
        if current_pool and current_start and current_end:
            wan = pool_to_wan(current_pool)
            if current_start == current_end:
                out[wan] = current_start
            else:
                out[wan] = f"{current_start}-{current_end}"
        current_pool = None
        current_start = None
        current_end = None

    for ln in lines:
        ln = ln.rstrip()

        m_pool = pool_start_re.match(ln)
        if m_pool:
            flush()
            current_pool = m_pool.group(1)
            continue

        m_s = startip_re.match(ln)
        if m_s:
            current_start = m_s.group(1)
            continue

        m_e = endip_re.match(ln)
        if m_e:
            current_end = m_e.group(1)
            continue

    flush()
    return out

def parse_firewall_address_transit_rows(unity_text: str, fieldnames: list[str]) -> list[dict]:
    """
    Lee firewall_address y crea filas extra para:
    - objetos con tags UnityTransit + UnityLAN  => tag = TransitLAN
    - objetos con solo UnityTransit             => tag = Transit

    Estructura esperada (como tu screenshot):
      firewall_address:
        - Management_lan:
            tagging:
              - Unity:
                  tags: "UnityTransit" "UnityLAN"
            subnet: 192.168.49.0 255.255.255.0
    """
    lines = extract_top_level_section_lines(unity_text, "firewall_address")

    entry_re = re.compile(r"^ {4}-\s+([^:]+):\s*$")  # - Name:
    tags_re = re.compile(r'^\s*tags:\s*(.+)\s*$')
    subnet_re = re.compile(r'^\s*subnet:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\s*$')

    def parse_tags_value(raw: str) -> list[str]:
        # Soporta: tags: "A" "B"  ó tags: [A,B] ó tags: A
        raw = raw.strip()
        quoted = re.findall(r'"([^"]+)"', raw)
        if quoted:
            return quoted
        # lista YAML simple
        m_list = re.match(r'^\[(.*)\]$', raw)
        if m_list:
            items = [x.strip().strip('"').strip("'") for x in m_list.group(1).split(",") if x.strip()]
            return items
        # fallback token
        return [raw.strip('"').strip("'")] if raw else []

    def blank_row_template() -> dict:
        # Para asegurar que las filas nuevas tengan TODAS las columnas del CSV
        return {k: "" for k in fieldnames}

    rows: list[dict] = []

    current_name = None
    current_tags: list[str] = []
    current_ip = ""
    current_mask = ""

    def flush():
        nonlocal current_name, current_tags, current_ip, current_mask
        if not current_name:
            return

        tagset = set(current_tags)

        if "UnityTransit" in tagset and "UnityLAN" in tagset:
            out_tag = "TransitLAN"
        #elif "UnityTransit" in tagset:
         #   out_tag = "Transit"
        else:
            # no nos interesa
            current_name = None
            current_tags = []
            current_ip = ""
            current_mask = ""
            return

        if not current_ip or not current_mask:
            # sin subnet no generamos fila
            current_name = None
            current_tags = []
            current_ip = ""
            current_mask = ""
            return

        row = blank_row_template()
        row["nombre"] = current_name
        row["tag"] = out_tag
        row["vlanid"] = ""
        row["ip"] = current_ip
        row["mask"] = current_mask
        row["gateway"] = ""

        # Estas columnas extra (si existen en tu CSV) se quedan vacías:
        # vpn / ip nat pool / dhcp range / Tech / etc.
        rows.append(row)

        # reset
        current_name = None
        current_tags = []
        current_ip = ""
        current_mask = ""

    for ln in lines:
        ln = ln.rstrip()

        m_entry = entry_re.match(ln)
        if m_entry:
            flush()
            current_name = m_entry.group(1).strip().strip('"')
            continue

        m_tags = tags_re.match(ln.strip())
        if m_tags and current_name:
            current_tags = parse_tags_value(m_tags.group(1))
            continue

        m_sub = subnet_re.match(ln.strip())
        if m_sub and current_name:
            current_ip = m_sub.group(1)
            current_mask = m_sub.group(2)
            continue

    flush()
    return rows

def detect_scenario_from_rows(rows: list[dict]) -> str:
    """
    Detecta Scenario según reglas del usuario.
    Ignora la interfaz Vsat_MGMT en la evaluación.
    Usa tags del CSV: UnityLAN, UnityWAN, TransitLAN, TransitWAN.
    """
    def norm(s: str) -> str:
        return (s or "").strip().lower()

    # Filas relevantes para scenario (ignoramos Vsat_MGMT)
    eval_rows = [r for r in rows if norm(r.get("nombre")) != "vsat_mgmt"]

    tags_present = set(norm(r.get("tag")) for r in eval_rows if r.get("tag"))
    # Normalizamos nombres de tags a forma canónica
    # (tu CSV usa exactamente "UnityLAN", "UnityWAN", "TransitLAN", "TransitWAN")
    # tags_present está en minúsculas.
    has_lan = "lan" in tags_present
    has_wan = "wan" in tags_present
    has_tlan = "transitlan" in tags_present
    has_twan = "transitwan" in tags_present

    # Detectar Fusion_Transit y su tag
    fusion_rows = [r for r in eval_rows if norm(r.get("nombre")) == "fusion_transit"]
    fusion_exists = len(fusion_rows) > 0
    fusion_is_lan = fusion_exists and norm(fusion_rows[0].get("tag")) == "lan"

    # Contar cuántas LAN hay (para diferenciar Scenario1.x vs Scenario2.5)
    lan_rows = [r for r in eval_rows if norm(r.get("tag")) == "lan"]
    lan_count = len(lan_rows)

    # ---------- Reglas (con prioridad de arriba a abajo) ----------

    # Scenario2: Solo existe una interfaz con tag LAN y se llama Fusion_Transit
    if fusion_exists and fusion_is_lan and lan_count == 1 and not has_wan and not has_tlan and not has_twan:
        return "Scenario2"

    # Scenario1: No existe Fusion_Transit y solo existen LAN y WAN (sin Transit)
    if not fusion_exists and not has_tlan and not has_twan and has_lan and has_wan:
        # Si quieres que exija LAN+WAN ambos, cambia a: if has_lan and has_wan ...
        return "Scenario1"

    # Scenario3.5: Existe Fusion_Transit y existen WAN, LAN, TransitLAN y TransitWAN
    if fusion_exists and fusion_is_lan and has_wan and not has_lan and has_tlan and has_twan:
        return "Scenario3.5"

    # Scenario1.x: Existe Fusion_Transit (LAN) y existen LAN, WAN, TransitLAN
    # (típicamente hay más LANs además de Fusion_Transit)
    if fusion_exists and fusion_is_lan and has_wan and has_lan and has_tlan and not has_twan and lan_count > 1:
        return "Scenario1.x"

    # Scenario2.5: Existe Fusion_Transit (LAN) y existen WAN y TransitLAN
    # Interpretación práctica: solo hay 1 LAN (Fusion_Transit), y además hay WAN y TransitLAN
    if fusion_exists and fusion_is_lan and has_wan and has_tlan and lan_count == 1 and not has_twan:
        return "Scenario2.5"

    # Scenario3: (tal como está escrito por ti es indistinguible de 2.5)
    # Lo dejamos como fallback si quieres forzarlo en algún caso adicional.
    if fusion_exists and fusion_is_lan and has_wan and not has_tlan and has_twan:
        return "Scenario3"

    return "Unknown"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unity", required=True, help="Unity finished config in yml-ish format (.conf/.yaml)")
    ap.add_argument("--vars", help="Input FG_vars.yml (template) - required only if you want to generate updated vars")
    ap.add_argument("--out", help="Output FG_vars.yml updated with Unity values - required with --vars")
    ap.add_argument("--csv-out", help="Optional: export UnityLAN/UnityWAN interfaces to CSV")
    args = ap.parse_args()

    unity_path = Path(args.unity)
    unity_text = unity_path.read_text(encoding="utf-8", errors="ignore")

    did_any = False

    if args.csv_out:
        export_unity_interfaces_csv(unity_text, Path(args.csv_out))
        did_any = True

    if args.vars or args.out:
        if not (args.vars and args.out):
            raise SystemExit("To generate FG_vars output, you must pass BOTH --vars and --out.")
        vars_path = Path(args.vars)
        out_path = Path(args.out)

        vars_data = yaml.safe_load(vars_path.read_text(encoding="utf-8", errors="ignore"))
        if not isinstance(vars_data, dict):
            raise SystemExit("FG_vars.yml must be a YAML mapping at top level.")

        updated = apply_updates(vars_data, unity_text)
        out_path.write_text(yaml.safe_dump(updated, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(f"OK: wrote {out_path}")
        did_any = True

    if not did_any:
        raise SystemExit("Nothing to do. Use --csv-out and/or --vars + --out.")

if __name__ == "__main__":
    main()
