import json, math
e = json.load(open('eq.json')); mult = round(10**(e['preamp']/20), 4)
nodes, links = [], []
for ch in 'LR':
    prev = f'pre{ch}'
    nodes.append(f'{{ type = builtin name = {prev} label = linear control = {{ Mult = {mult} Add = 0.0 }} }}')
    for i, p in enumerate(e['peq']):
        n = f'{ch.lower()}{i+1}'
        nodes.append(f'{{ type = builtin name = {n} label = bq_peaking control = {{ Freq = {p["f"]} Q = {p["Q"]} Gain = {p["gain"]} }} }}')
        links.append(f'{{ output = "{prev}:Out" input = "{n}:In" }}'); prev = n
    last = prev
args = f'''{{
  node.description = "Audiohub EQ"
  media.name = "Audiohub EQ"
  filter.graph = {{
    nodes = [ {' '.join(nodes)} ]
    links = [ {' '.join(links)} ]
    inputs = [ "preL:In" "preR:In" ]
    outputs = [ "l{len(e['peq'])}:Out" "r{len(e['peq'])}:Out" ]
  }}
  audio.channels = 2
  audio.position = [ FL FR ]
  capture.props = {{ node.name = "audiohub_eq" node.description = "Audiohub EQ" media.class = Audio/Sink }}
  playback.props = {{ node.name = "audiohub_eq_out" target.object = "bluez_output.74_68_59_68_28_17.1" node.dont-fallback = true node.passive = true }}
}}'''
open('eq.args','w').write(args); print(args[:200], '...')
