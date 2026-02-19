**🧠 Unity to FG CSV Generator**

Automates the parsing of Unity-generated FortiOS YAML configurations and generates a structured CSV including:

- Interface classification (LAN / WAN / Transit)
- SD-WAN gateway mapping
- NAT pools
- DHCP ranges and exclusions
- Tech extraction (UnityTech*)
- Automatic Scenario detection


**📌 Overview**

This tool processes Unity YAML exports and produces a normalized CSV file used for:

- Technical validation
- Deployment analysis
- Scenario classification
- Migration workflows
- Documentation generation


It is designed for OmniAccess internal use and follows Unity tagging conventions.

**⚙️ Features**


✔ Parses system_interface
✔ Parses system_sdwan.members
✔ Parses firewall_ippool
✔ Parses system_dhcp_server
✔ Parses firewall_address
✔ Classifies TransitWAN via suffix rule
✔ Generates synthetic TransitLAN entries
✔ Applies Fusion_Transit gateway override (+1 logic)
✔ Detects deployment Scenario automatically

**🏗 Architecture**

```
Unity YAML
   │
   ├── system_interface
   ├── system_sdwan
   ├── firewall_ippool
   ├── system_dhcp_server
   └── firewall_address
          │
          ▼
  Parsing & Classification Layer
          │
          ▼
  Data Enrichment Layer
          │
          ▼
  Scenario Engine
          │
          ▼
        CSV Output
```


**🚀 Usage**

**Requirements**

- Python 3.9+
- No external dependencies


Check Python version:

`python3 --version`

**Basic Execution**

```
python3 unity_to_fg_vars.py \
  --unity FG-Unity-XXXX.yaml \
  --csv-out interfaces_output.csv
```


**📤 Output**

The script generates a CSV file with the following columns:

```
nombre
tag
vlanid
ip
mask
gateway
ip nat pool
dhcp range
dhcp excluded
Tech
vpn
Scenario
```


**🧩 Interface Classification Rules**

| Condition | Result |	
| ------ | ------ |
| role = lan | UnityLAN |
| role = wan | UnityWAN |
| name ends with "Transit" (except Fusion_Transit) | TransitWAN |	
| firewall_address with UnityTransit + UnityLAN | TransitLAN |	


**🔄 Special Logic**

**Fusion_Transit Gateway Rule**

For:

- Fusion_Transit
- TransitLAN interfaces


Gateway is automatically set to:

`Fusion_Transit IP + 1`

**Tech Column**

If a WAN contains a tag:

`UnityTechVSAT`


The CSV shows:

`VSAT`

**Scenario Detection**

The script automatically classifies the configuration into:

- Scenario1
- Scenario1.x
- Scenario2
- Scenario2.5
- Scenario3
- Scenario3.5


Vsat_MGMT is ignored during scenario evaluation.

```
📂 Repository Structure
.
├── unity_to_fg_vars_with_csv.py
├── README.md
└── examples/
```


**🧪 Testing**


Recommended test cases:

| Fusion_Transit | TransitLAN | TransitWAN | Expected Scenario |
|----------------|------------|------------|-------------------|
| No | No | No | Scenario1 |
| Yes | No | No | Scenario2 |
| Yes | Yes | No | Scenario1.x |
| Yes | Yes | Yes | Scenario3.5 |

**🛠 Troubleshooting**

**Missing Section Errors**

If the YAML lacks:

- firewall_ippool
- system_dhcp_server
- system_sdwan

The script continues and leaves related fields empty.

**Scenario = Unknown**

Check:

- Tags classification
- Presence of Fusion_Transit
- TransitWAN vs TransitLAN detection


**🔒 Assumptions**

- Unity YAML indentation is consistent
- Only one Fusion_Transit exists
- Tags follow Unity naming conventions
- Roles are strictly lan or wan


**🔮 Future Improvements**

- Unit test suite
- Logging framework
- JSON export option
- Debug mode
- CI integration

**👥 Maintainers**

OmniAccess – Automation Team
