#!/usr/bin/env python
# coding=utf-8

import grpc
from datetime import datetime
import sys
import logging
import socket
import os
from ipaddress import ip_network, ip_address, IPv4Address
import json
import signal
import traceback
import re
import time
# from concurrent.futures import ThreadPoolExecutor
from threading import Thread

import sdk_service_pb2
import sdk_service_pb2_grpc
import config_service_pb2

# To report state back
import telemetry_service_pb2
import telemetry_service_pb2_grpc

from pygnmi.client import gNMIclient, telemetryParser
from logging.handlers import RotatingFileHandler

# PeeringDB integration
import typing, requests, netns
# from urllib.parse import quote

############################################################
## Agent will start with this name
############################################################
agent_name='ixp_agent'

acl_sequence_start=1000 # Default ACL sequence number base, can be configured
acl_count=0             # Number of ACL entries created/managed

ixp = ""                # IXP site, e.g. "DE-CIX Frankfurt"
peer_as_list = []       # List of AS to peer with

############################################################
## Open a GRPC channel to connect to sdk_mgr on the dut
## sdk_mgr will be listening on 50053
############################################################
channel = grpc.insecure_channel('unix:///opt/srlinux/var/run/sr_sdk_service_manager:50053')
# channel = grpc.insecure_channel('127.0.0.1:50053')
metadata = [('agent_name', agent_name)]
stub = sdk_service_pb2_grpc.SdkMgrServiceStub(channel)

match_port = { 0: 'source-port', 1: 'destination-port' }

############################################################
## Subscribe to required event
## This proc handles subscription of: Interface, LLDP,
##                      Route, Network Instance, Config
############################################################
def Subscribe(stream_id, option):
    op = sdk_service_pb2.NotificationRegisterRequest.AddSubscription
    if option == 'cfg':
        entry = config_service_pb2.ConfigSubscriptionRequest()
        entry.key.js_path = '.' + agent_name # filter out .commit.end notifications
        request = sdk_service_pb2.NotificationRegisterRequest(op=op, stream_id=stream_id, config=entry)

    subscription_response = stub.NotificationRegister(request=request, metadata=metadata)
    print('Status of subscription response for {}:: {}'.format(option, subscription_response.status))

############################################################
## Subscribe to all the events that Agent needs
############################################################
def Subscribe_Notifications(stream_id):
    '''
    Agent will receive notifications to what is subscribed here.
    '''
    if not stream_id:
        logging.info("Stream ID not sent.")
        return False

    # Subscribe to config changes, first
    Subscribe(stream_id, 'cfg')

"""
Lookup ASN in PeeringDB and return ipv4,ipv6 peering IPs at given IX
"""
def query_peeringdb(asn: int, ix: str) -> typing.Tuple[typing.Optional[str],typing.Optional[str],typing.Optional[str]]:
  while not os.path.exists('/var/run/netns/srbase-mgmt'):
    logging.info("Waiting for srbase-default netns to be created...")
    time.sleep(2) # 1 second is not enough

  with netns.NetNS(nsname="srbase-mgmt"):
    url = f"https://peeringdb.com/api/netixlan?asn={asn}&name__contains={ix.replace(' ','%20')}"
    logging.info( f"PeeringDB query: {url}" )
    resp = requests.get(url=url)
  pdb_json = json.loads(resp.text)
  print( pdb_json )
  if 'data' in pdb_json and pdb_json['data']:
    site = pdb_json['data'][0]
    return ( site['name'], site['ipaddr4'], site['ipaddr6'] )
  return ( None, None, None )

def get_prefixlist(asn: int):
  """
  Retrieve list of prefixes registered in IRR for the given AS
  """
  with netns.NetNS(nsname="srbase-mgmt"):
    url = f"https://irrexplorer.nlnog.net/api/prefixes/asn/AS{asn}"
    logging.info( f"irrexplorer query: {url}" )
    resp = requests.get(url=url)

  pfl_json = json.loads(resp.text)
  # Could use bgpOrigins (AS list) too
  return [ i["prefix"] for i in pfl_json["overlaps"] if i["goodnessOverall"]==1 ]

def ConfigureBGPPeering():
    PATH = '/network-instance[name=default]/protocols/bgp'
    with gNMIclient(target=('unix:///opt/srlinux/var/run/sr_gnmi_server',57400),
                            username="admin",password="NokiaSrl1!",
                            insecure=True, debug=False) as gnmi:

     def addPeer(_as,name,ip,af,pfx):
      group_name = f"ix-{af}"
      policy_name = f"ix-import-{_as}-{af}"
      updates = [ (f"/routing-policy/policy[name={policy_name}]",
       {
        "default-action": {
          "policy-result": "reject"
        },
        "statement": [
        {
          "name": "irr",
          "match": {
            "prefix-set": f"as{_as}-{af}"
          },
          "action": {
            "policy-result": "accept"
          }
        }
        ]
       }
       ) ]
      prefixes = []
      for p in pfx:
       if ((af=='ipv4' and '.' in p) or (af=='ipv6' and ':' in p)):
        prefixes.append( { "ip-prefix": p, "mask-length-range": "exact" } )
      updates.append( (f'/routing-policy/prefix-set[name=as{_as}-{af}]', {"prefix": prefixes}) )

      updates.append( (PATH+f'/group[group-name={group_name}]',
       {
        "admin-state": "enable",
        "description": name, # from PeeringDB
        "afi-safi": [
          {
            "afi-safi-name": "ipv4-unicast",
            "admin-state": "disable" if af=="ipv6" else "enable"
          },
          {
            "afi-safi-name": "ipv6-unicast",
            "admin-state": "disable" if af=="ipv4" else "enable"
          }
         ]
       })
      )
      updates.append( (PATH+f'/neighbor[peer-address={ip}]',
       {
          "peer-as": _as,
          "peer-group": group_name,
          "import-policy": policy_name

          # Could query https://www.peeringdb.com/api/net?asn=x and set max-prefixes
       })
      )
      gnmi.set( encoding='json_ietf', update=updates )

     for peer in peer_as_list:
      (name,ip4,ip6) = query_peeringdb( peer, ixp )
      logging.info( f"PeeringDB result: {name} {ip4} {ip6}" )
      if ip4 or ip6:
       pfx = get_prefixlist(peer)
       logging.info( f"Prefix count: {len(pfx)}" )
       if ip4:
        addPeer(peer,name,ip4,"ipv4",pfx)
       if ip6:
        addPeer(peer,name,ip6,"ipv6",pfx)

##################################################################
## Proc to process the config Notifications received by auto_config_agent
## At present processing config from js_path = .fib-agent
##################################################################
def Handle_Notification(obj):
    if obj.HasField('config'):
        logging.info(f"GOT CONFIG :: {obj.config.key.js_path}")
        if ".ixp_agent" in obj.config.key.js_path:
            logging.info(f"Got config for agent, now will handle it :: \n{obj.config}\
                            Operation :: {obj.config.op}\nData :: {obj.config.data.json}")
            if obj.config.op == 2:
                logging.info(f"Delete ixp-agent cli scenario")
                # if file_name != None:
                #    Update_Result(file_name, action='delete')
                response=stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
                logging.info('Handle_Config: Unregister response:: {}'.format(response))
            else:
                json_acceptable_string = obj.config.data.json.replace("'", "\"")
                data = json.loads(json_acceptable_string)
                if 'acl_sequence_start' in data:
                    global acl_sequence_start
                    acl_sequence_start = int( data['acl_sequence_start']['value'] )
                    logging.info(f"Got init sequence :: {acl_sequence_start}")

                if 'IXP' in data:
                    global ixp
                    ixp = data['IXP']['value']
                if 'peer_as' in data:
                    logging.info(f"Peer AS list : {data['peer_as']}")
                    global peer_as_list
                    peer_as_list = [ int(e['value']) for e in data['peer_as'] ]

                try:
                  ConfigureBGPPeering()
                except Exception as e:
                  logging.error(e)
                  sys.exit(1)
                return True

    else:
        logging.info(f"Unexpected notification : {obj}")

    return False

def Gnmi_subscribe_bgp_changes():
    logging.info( "Gnmi_subscribe_bgp_changes -> start subscription to BGP neighbor events" )
    subscribe = {
            'subscription': [
                {
                    # 'path': '/srl_nokia-network-instance:network-instance[name=*]/protocols/srl_nokia-bgp:bgp/neighbor[peer-address=*]/admin-state',
                    # Possible to subscribe without '/admin-state', but then too many events
                    # Like this, no 'delete' is received when the neighbor is deleted
                    # Also, 'enable' event is followed by 'disable' - broken
                    # 'path': '/network-instance[name=*]/protocols/bgp/neighbor[peer-address=*]/admin-state',
                    # This leads to too many events, hitting the max 60/minute gNMI limit
                    # 10 events per CLI change to a bgp neighbor, many duplicates
                    # 'path': '/network-instance[name=*]/protocols/bgp/neighbor[peer-address=*]',
                    'path': '/network-instance[name=*]/protocols/bgp/neighbor[peer-address=*]',
                    'mode': 'on_change',
                    # 'heartbeat_interval': 10 * 1000000000 # ns between, i.e. 10s
                    # Mode 'sample' results in polling
                    # 'mode': 'sample',
                    # 'sample_interval': 10 * 1000000000 # ns between samples, i.e. 10s
                },
                {  # Also monitor dynamic-neighbors sections
                   'path': '/network-instance[name=*]/protocols/bgp/dynamic-neighbors/accept/match[prefix=*]',
                   'mode': 'on_change',
                }
            ],
            'use_aliases': False,
            'mode': 'stream',
            'encoding': 'json'
        }
    _bgp = re.compile( r'^network-instance\[name=([^]]*)\]/protocols/bgp/neighbor\[peer-address=([^]]*)\]/.*$' )
    _dyn = re.compile( r'^network-instance\[name=([^]]*)\]/protocols/bgp/dynamic-neighbors/accept/match\[prefix=([^]]*)\]/.*$' )

    connected = False
    while not connected:
      try:
        # with Namespace('/var/run/netns/srbase-mgmt', 'net'):
        with gNMIclient(target=('unix:///opt/srlinux/var/run/sr_gnmi_server',57400),
                                username="admin",password="NokiaSrl1!",
                                insecure=True, debug=False) as c:
          connected = True
          telemetry_stream = c.subscribe(subscribe=subscribe)
          logging.info( "Unix socket connected...waiting for subscribed gNMI events" )
          for m in telemetry_stream:
            try:
              if m.HasField('update'): # both update and delete events
                  # Filter out only toplevel events
                  parsed = telemetryParser(m)
                  logging.info(f"gNMI change event :: {parsed}")
                  update = parsed['update']
                  if update['update']:
                     path = update['update'][0]['path']  # Only look at top level
                     neighbor = _bgp.match( path )
                     if neighbor:
                        net_inst = neighbor.groups()[0]
                        ip_prefix = neighbor.groups()[1] # plain ip
                        peer_type = "static"
                        logging.info(f"Got neighbor change event :: {ip_prefix}")
                     else:
                        dyn_group = _dyn.match( path )
                        if dyn_group:
                           net_inst = dyn_group.groups()[0]
                           ip_prefix = dyn_group.groups()[1] # ip/prefix
                           peer_type = "dynamic"
                           logging.info(f"Got dynamic-neighbor change event :: {ip_prefix}")
                        else:
                          logging.info(f"Ignoring gNMI change event :: {path}")
                          continue

                     # No-op if already exists
                     Add_ACL(c,ip_prefix.split('/'),net_inst,peer_type)
                  else: # pygnmi does not provide 'path' for delete events
                     handleDelete(c,m)

            except Exception as e:
              traceback_str = ''.join(traceback.format_tb(e.__traceback__))
              logging.error(f'Exception caught in gNMI :: {e} m={m} stack:{traceback_str}')
      except grpc.FutureTimeoutError as e:
        logging.error( e )
        time.sleep( 5 )
    logging.info("Leaving BGP event loop")

def handleDelete(gnmi,m):
    logging.info(f"handleDelete :: {m}")
    for e in m.update.delete:
       for p in e.elem:
         # TODO dynamic-neighbors, also modify of prefix in dynamic-neighbors
         if p.name == "neighbor":
           for n,v in p.key.items():
             logging.info(f"n={n} v={v}")
             if n=="peer-address":
                peer_ip = v
                Remove_ACL(gnmi,peer_ip)
                # return # Can be multiple entries

#
# Checks if this is an IPv4 or IPv6 address, and normalizes host prefixes
#
def checkIP( ip_prefix ):
    try:
        v = 4 if type(ip_address(ip_prefix[0])) is IPv4Address else 6
        prefix = ip_prefix[1] if len(ip_prefix)>1 else ('32' if v==4 else '128')
        return v, ip_prefix[0], prefix
    except ValueError:
        return None

def Add_Telemetry(js_path, dict):
    telemetry_stub = telemetry_service_pb2_grpc.SdkMgrTelemetryServiceStub(channel)
    telemetry_update_request = telemetry_service_pb2.TelemetryUpdateRequest()
    telemetry_info = telemetry_update_request.state.add()
    telemetry_info.key.js_path = js_path
    telemetry_info.data.json_content = json.dumps(dict)
    logging.info(f"Telemetry_Update_Request :: {telemetry_update_request}")
    telemetry_response = telemetry_stub.TelemetryAddOrUpdate(request=telemetry_update_request, metadata=metadata)
    logging.info(f"TelemetryAddOrUpdate response:{telemetry_response}")
    return telemetry_response

def Update_ACL_Counter(delta):
    global acl_count
    acl_count += delta
    _ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    Add_Telemetry( ".ixp_agent", { "acl_count"   : acl_count,
                                       "last_change" : _ts } )

def Add_ACL(gnmi,ip_prefix,net_inst,peer_type):
    seq, next_seq, v, ip, prefix = Find_ACL_entry(gnmi,ip_prefix) # Also returns next available entry
    if seq is None:
        updates = []
        for i in range(0,2):
          acl_entry = {
           "created-by-ixp-agent": datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC"),
           "description": f"BGP ({peer_type}) peer in network-instance {net_inst}",
           "match": {
             ("protocol" if v==4 else "next-header"): "tcp",
             "source-ip": { "prefix": ip + '/' + prefix },
             match_port[i] : { "operator": "eq", "value": 179 }
           },
           "action": { "accept": { } },
          }
          path = f'/acl/cpm-filter/ipv{v}-filter/entry[sequence-id={next_seq+i}]'
          logging.info(f"Update: {path}={acl_entry}")
          updates.append( (path,acl_entry) )
        gnmi.set( encoding='json_ietf', update=updates )

        # Need to set state separately, not via gNMI. Uses underscores in path
        # Tried extending ACL entries, but system won't accept these updates
        # js_path = (f'.acl.cpm_filter.ipv{v}_filter.entry' +
        #            '{.sequence_id==' + str(next_seq) + '}.bgp_acl_agent_state')
        # js_path = '.bgp_acl_agent.entry{.ip=="'+peer_ip+'"}'
        # Add_Telemetry( js_path, { "sequence_id": next_seq } )
        Update_ACL_Counter( +2 )

def Remove_ACL(gnmi,peer_ip):
   seq, next_seq, v, ip, prefix = Find_ACL_entry(gnmi,[peer_ip])
   if seq is not None:
       logging.info(f"Remove_ACL: Deleting ACL entry :: {seq}")
       path = f'/acl/cpm-filter/ipv{v}-filter/entry[sequence-id={seq}]'
       gnmi.set( encoding='json_ietf', delete=[path] )
       Update_ACL_Counter( -1 )
   else:
       logging.info(f"Remove_ACL: No entry found for peer_ip={peer_ip}")

#
# Because it is possible that ACL entries get saved to 'startup', the agent may
# not have a full map of sequence number to peer_ip. Therefore, we perform a
# lookup based on IP address each time
# Since 'prefix' is not a key, we have to loop through all entries with a prefix
#
def Find_ACL_entry(gnmi,ip_prefix):
   v, ip, prefix = checkIP( ip_prefix )

   #
   # Can do it like this and add custom state, but then we cannot find the next
   # available sequence number we can use
   # path = f"/bgp-acl-agent/entry[ip={peer_ip}]"
   path = f'/acl/cpm-filter/ipv{v}-filter/entry/match/'
   # could add /source-ip/prefix but then we cannot check for dest-port

   # Could filter like this to reduce #entries, limits to max 999 entries
   # path = '/acl/cpm-filter/ipv4-filter/entry[sequence-id=1*]/match

   # Interestingly, datatype='config' is required to see custom config state
   # The default datatype='all' does not show it
   acl_entries = gnmi.get( encoding='json_ietf', path=[path] )
   logging.info(f"Find_ACL_entry({ip_prefix}): GOT gNMI GET response")
   searched = ip + '/' + prefix
   global acl_sequence_start
   next_seq = acl_sequence_start
   for e in acl_entries['notification']:
     try:
      if 'update' in e:
        logging.info(f"GOT Update :: {e['update']}")
        for u in e['update']:
            for j in u['val']['entry']:
               logging.info(f"Check ACL entry :: {j}")
               match = j['match']
               # Users could change acl_sequence_start
               for i in range(0,2):
                if "source-ip" in match: # and j['sequence-id'] >= acl_sequence_start:
                  src_ip = match[ "source-ip" ]
                  if 'prefix' in src_ip:
                     if (src_ip['prefix'] == searched):
                       logging.info(f"Find_ACL_entry: Found matching entry :: {j}")
                       # Perform extra sanity check
                       key_name = match_port[i]
                       if (key_name in match
                            and 'value' in match[key_name]
                            and match[key_name]['value'] == 179):
                           return (j['sequence-id'],None,v,ip,prefix)
                       else:
                           logging.info( "Source/Dest IP match but not BGP port" )
                     if j['sequence-id']==next_seq:
                       logging.info( f"Increment next_seq (={next_seq})" )
                       next_seq += 1
     except Exception as e:
        logging.error(f'Exception caught in Find_ACL_entry :: {e}')
   logging.info(f"Find_ACL_entry: no match for searched={searched} next_seq={next_seq}")
   return (None,next_seq,v,ip,prefix)

##################################################################################################
## This is the main proc where all processing for auto_config_agent starts.
## Agent registration, notification registration, Subscrition to notifications.
## Waits on the subscribed Notifications and once any config is received, handles that config
## If there are critical errors, Unregisters the fib_agent gracefully.
##################################################################################################
def Run():
    sub_stub = sdk_service_pb2_grpc.SdkNotificationServiceStub(channel)

    # On startup, wait a few seconds before registering to make sure DNS/BGP config is in place
    logging.info("Waiting 30s to let DNS/BGP config settle in...")
    time.sleep(30)

    response = stub.AgentRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
    logging.info(f"Registration response : {response.status}")

    request=sdk_service_pb2.NotificationRegisterRequest(op=sdk_service_pb2.NotificationRegisterRequest.Create)
    create_subscription_response = stub.NotificationRegister(request=request, metadata=metadata)
    stream_id = create_subscription_response.stream_id
    logging.info(f"Create subscription response received. stream_id : {stream_id}")

    Subscribe_Notifications(stream_id)

    stream_request = sdk_service_pb2.NotificationStreamRequest(stream_id=stream_id)
    stream_response = sub_stub.NotificationStream(stream_request, metadata=metadata)

    # Pause for now
    # Thread( target=Gnmi_subscribe_bgp_changes ).start()

    try:
        for r in stream_response:
            logging.info(f"NOTIFICATION:: \n{r.notification}")
            for obj in r.notification:
                Handle_Notification(obj)

    except grpc._channel._Rendezvous as err:
        logging.info(f'GOING TO EXIT NOW: {err}')

    except Exception as e:
        logging.error(f'Exception caught :: {e}')
    finally:
        Exit_Gracefully(0,0)
    return True
############################################################
## Gracefully handle SIGTERM signal
## When called, will unregister Agent and gracefully exit
############################################################
def Exit_Gracefully(signum, frame):
    logging.info( f"Caught signal :: {signum}\n will unregister bgp acl agent" )
    try:
        response=stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
        logging.info( f'Exit_Gracefully AgentUnRegister response:: {response}' )
    except grpc._channel._Rendezvous as err:
        logging.info( f'_Rendezvous error - GOING TO EXIT NOW: {err}' )
    finally:
        sys.exit()

##################################################################################################
## Main from where the Agent starts
## Log file is written to: /var/log/srlinux/stdout/bgp_acl_agent.log
## Signals handled for graceful exit: SIGTERM
##################################################################################################
if __name__ == '__main__':
    # hostname = socket.gethostname()
    stdout_dir = '/var/log/srlinux/stdout' # PyTEnv.SRL_STDOUT_DIR
    signal.signal(signal.SIGTERM, Exit_Gracefully)
    if not os.path.exists(stdout_dir):
        os.makedirs(stdout_dir, exist_ok=True)
    log_filename = f'{stdout_dir}/{agent_name}.log'
    logging.basicConfig(
      handlers=[RotatingFileHandler(log_filename, maxBytes=3000000,backupCount=5)],
      format='%(asctime)s,%(msecs)03d %(threadName)s %(levelname)s %(message)s',
      datefmt='%H:%M:%S', level=logging.INFO)
    logging.info("START TIME :: {}".format(datetime.now()))
    Run()
