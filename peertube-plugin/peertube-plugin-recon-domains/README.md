# peertube-plugin-recon-domains

Registers 18 RECON knowledge domains as PeerTube video categories using IDs 100-117. These categories are assigned automatically by RECON's domain assignment pipeline based on concept extraction analysis.

## Category Mapping

| ID  | Domain                    |
|-----|---------------------------|
| 100 | Agriculture & Livestock   |
| 101 | Civil Organization        |
| 102 | Communications            |
| 103 | Food Systems              |
| 104 | Foundational Skills       |
| 105 | Logistics                 |
| 106 | Medical                   |
| 107 | Navigation                |
| 108 | Operations                |
| 109 | Power Systems             |
| 110 | Preservation & Storage    |
| 111 | Security                  |
| 112 | Shelter & Construction    |
| 113 | Technology                |
| 114 | Tools & Equipment         |
| 115 | Vehicles                  |
| 116 | Water Systems             |
| 117 | Wilderness Skills         |

Built-in PeerTube categories (IDs 1-18) are not modified.

## Install

```bash
# Copy plugin to PeerTube storage
cp -r peertube-plugin-recon-domains /var/www/peertube/storage/plugins/node_modules/

# Register plugin via API or admin UI
# Admin > Plugins > Install > peertube-plugin-recon-domains

# Restart PeerTube
sudo systemctl restart peertube
```

## Uninstall

Remove the plugin via PeerTube admin UI or:

```bash
rm -rf /var/www/peertube/storage/plugins/node_modules/peertube-plugin-recon-domains
sudo systemctl restart peertube
```

Videos with RECON categories will revert to showing the raw category ID until the plugin is reinstalled.
