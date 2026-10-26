"""Native chapter tables for DVD titles containing manually advanced stills.

The playlist reuses the existing clips. The DVD VM retains control of waits,
cell commands and PGC transitions; native chapter seeks select original PTTs.
"""
from pathlib import Path
from .bdmv import joined_playlist


def plans(graph):
    pgcs={p['key']:p for p in graph['pgcs']}
    owners={}
    for title in graph['titles']:
        for part in title.get('parts',[]):
            owners.setdefault((title['vts'],part['pgc']),set()).add(title['number'])
    result=[]
    for title in graph['titles']:
        parts=title.get('parts',[])
        if len(parts)<2:continue
        order=list(dict.fromkeys(part['pgc'] for part in parts))
        rows=[pgcs[f"T:{title['vts']}:{n}"] for n in order]
        if sum(c['still']==255 for p in rows for c in p['cells'])<2:continue
        # A shared PGC cannot have two different native chapter tables. Keep
        # such branching titles on their existing DVD-VM path.
        if any(len(owners[(title['vts'],n)])!=1 for n in order):continue
        expected=[dict(pgc=n,program=i+1) for n,p in zip(order,rows) for i in range(len(p['programs']))]
        if parts!=expected or any(not p['cells'] or p['playback_mode'] for p in rows):continue
        if any(c.get('playlist',-1)<0 or c['duration']<=0 or c.get('block_type',0) or c.get('interleaved',False)
               for p in rows for c in p['cells']):continue
        result.append((title,rows))
    return result


def install(root,graph,report):
    root=Path(root);records=[]
    next_id=max(int(p.stem) for p in (root/'BDMV/PLAYLIST').glob('*.mpls'))+1
    for title,pgcs in plans(graph):
        cells=[c for p in pgcs for c in p['cells']]
        if next_id>=2000:raise ValueError('Too many BD-J playlists')
        ids=[c.get('clip',c['playlist']) for c in cells]
        sources=[];items=[];chapters=[];holds=[]
        for pgc in pgcs:
            for cell in pgc['cells']:
                clip=cell.get('clip',cell['playlist'])
                source=(root/f'BDMV/PLAYLIST/{clip:05d}.mpls').read_bytes()
                if cell['number'] in pgc['programs']:chapters.append(len(sources)+1)
                items.append(len(sources));sources.append(source)
                held=cell['still']!=0 or (cell['number']==len(pgc['cells']) and pgc['still']!=0)
                holds.append(held)
                if held:
                    # libbluray tracks chapters by consumed source packets. At
                    # an infinite still, EOF otherwise equals the NEXT chapter's
                    # start and Next skips a picture. An unmarked reuse of this
                    # same held clip separates those packet positions. The VM
                    # always seeks over it; no additional media is written.
                    sources.append(source)
        data,times=joined_playlist(sources,chapters)
        for prefix in ('','BACKUP/'):
            (root/f'BDMV/{prefix}PLAYLIST/{next_id:05d}.mpls').write_bytes(data)
        starts=[];ticks=0
        for begin,end in times:
            starts.append(ticks);ticks+=end-begin
        for cell,clip,item in zip(cells,ids,items):
            begin,end=times[item]
            if end<=begin:raise ValueError('Empty gallery play item')
            ticks=starts[item]
            cell.update(clip=clip,playlist=next_id,start_ms=round(ticks/45),
                        end_ms=round((ticks+end-begin)/45),native_item=item)
        records.append(dict(title=title['number'],playlist=next_id,pgcs=[p['key'] for p in pgcs],
                            chapters=len(chapters),clips=ids,items=items,guard_items=[n+1 for n,h in zip(items,holds) if h]))
        next_id+=1
    superseded={key for row in records for key in row['pgcs']}
    report['joined_title_playlists']=[row for row in report.get('joined_title_playlists',[]) if row['pgc'] not in superseded]
    report['native_gallery_playlists']=records
    return records


def verify(root,graph,report):
    """Check item reuse, PTT order, still flags and runtime offsets separately."""
    root=Path(root);pgcs={p['key']:p for p in graph['pgcs']};count=0
    for row in report.get('native_gallery_playlists',[]):
        title=next(t for t in graph['titles'] if t['number']==row['title'])
        groups=[pgcs[key] for key in row['pgcs']]
        cells=[c for p in groups for c in p['cells']]
        path=f"BDMV/PLAYLIST/{row['playlist']:05d}.mpls";data=(root/path).read_bytes()
        if data!=(root/path.replace('BDMV/','BDMV/BACKUP/')).read_bytes():raise ValueError('Gallery backup differs')
        start=int.from_bytes(data[8:12],'big');mark=int.from_bytes(data[12:16],'big')
        size=int.from_bytes(data[start+6:start+8],'big');pos=start+10;items=[];offset=0
        for index in range(size):
            end=pos+2+int.from_bytes(data[pos:pos+2],'big')
            if end>len(data):raise ValueError('Truncated gallery play item')
            begin_time=int.from_bytes(data[pos+14:pos+18],'big');end_time=int.from_bytes(data[pos+18:pos+22],'big')
            items.append((bytes(data[pos:end]),offset,end_time-begin_time))
            offset+=end_time-begin_time;pos=end
        expected_marks=[];expected_items=[]
        for pgc in groups:
            for cell in pgc['cells']:
                index=cell['native_item'];expected_items.append(index)
                if cell['playlist']!=row['playlist'] or index!=len(expected_items)-1+sum(i<index for i in row['guard_items']):
                    raise ValueError('Gallery runtime play-item mapping changed')
                item,at,duration=items[index]
                source=(root/f"BDMV/PLAYLIST/{cell['clip']:05d}.mpls").read_bytes()
                sp=int.from_bytes(source[8:12],'big')+10;se=sp+2+int.from_bytes(source[sp:sp+2],'big')
                original=bytearray(source[sp:se]);original[12]=(original[12]&0xf0)|1
                if item!=bytes(original):raise ValueError('Gallery changed physical clip metadata')
                if (cell['start_ms'],cell['end_ms'])!=(round(at/45),round((at+duration)/45)):
                    raise ValueError('Gallery runtime presentation offsets changed')
                held=cell['still']!=0 or (cell['number']==len(pgc['cells']) and pgc['still']!=0)
                if held:
                    if item[31]!=2 or items[index+1][0]!=item:raise ValueError('Gallery still/guard does not retain the picture')
                for part in title['parts']:
                    if part['pgc']==int(pgc['key'].rsplit(':',1)[1]) and pgc['programs'][part['program']-1]==cell['number']:
                        expected_marks.append((index,int.from_bytes(item[14:18],'big')))
        marks=int.from_bytes(data[mark+4:mark+6],'big')
        found=[(int.from_bytes(data[mark+8+n*14:mark+10+n*14],'big'),int.from_bytes(data[mark+10+n*14:mark+14+n*14],'big')) for n in range(marks)]
        if found!=expected_marks or marks!=len(title['parts']):raise ValueError('Gallery changed original native chapter order')
        if expected_items!=row['items'] or len(items)!=len(cells)+len(row['guard_items']):raise ValueError('Gallery item inventory changed')
        count+=1
    return count
