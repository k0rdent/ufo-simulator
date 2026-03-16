# Heat deployment

## Management network IP allocation

The management network uses the **10.200.0.0/24** subnet. The following table describes how addresses in this range are allocated and reserved.

| Address or range           | Purpose                          |
|---------------------------|----------------------------------|
| 10.200.0.1                | Gateway                          |
| 10.200.0.2                | VNC                              |
| 10.200.0.3                | SDLC                             |
| 10.200.0.100 – 10.200.0.150 | Reserved for switches (manual IPs) |
| 10.200.0.200 – 10.200.0.254 | Reserved for SDLC DHCP allocation  |
