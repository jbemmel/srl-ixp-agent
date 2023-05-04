# srl-ixp-agent
An agent to auto-configure BGP peering and prefix policies based on PeeringDB and IRR queries

## Details
The YANG configuration model consists of an IXP site name and a list of AS numbers to peer with.
The agent will query https://peeringdb.com/api/netixlan?asn={asn} to determine the IP addresses (ipv4/ipv6) for each peer AS,
and then generate the BGP configuration.

In addition, the agent will query https://irrexplorer.nlnog.net/api/prefixes/asn/AS{asn} to get a list of IPv4/6 prefixes,
and it provisions a filter policy to accept only those prefixes

Demo topology: https://github.com/jbemmel/netsim-examples/tree/master/BGP/IXP-Peering

## Build instructions

```
make rpm
```
