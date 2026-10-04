from __future__ import annotations
import argparse
import json
from pathlib import Path
from .source import Disc
from .ifo import inspect
from .author import author,eligibility,unique_cells,build_java,run
from .binary import model


def main(argv=None):
    parser=argparse.ArgumentParser(description="Experimental DVD2HEVC to BD-J/BDMV converter")
    sub=parser.add_subparsers(dest="command",required=True)
    scan=sub.add_parser("inspect",help="Read original PGCs, commands, chapters, cells and stream maps")
    scan.add_argument("source",type=Path)
    scan.add_argument("--output",type=Path)
    build=sub.add_parser("author",help="Author a new experimental BDMV directory")
    build.add_argument("source",type=Path);build.add_argument("output",type=Path)
    for opt in ("tsmuxer","ffmpeg","ffprobe","bdj-api"):build.add_argument("--"+opt,type=Path)
    build.add_argument("--max-cells",type=int,help="Debug only: partial disc, missing routes fail visibly")
    build.add_argument("--menus-only",action="store_true",help="Debug only: omit title media")
    trace=sub.add_parser("trace",help="Run the BD-J navigator headlessly on an inspected DVD")
    trace.add_argument("source",type=Path);trace.add_argument("actions",nargs="*")
    trace.add_argument("--work",type=Path,default=Path("work/trace"))
    trace.add_argument("--bdj-api",type=Path)
    iso=sub.add_parser("iso",help="Create and independently verify a UDF 2.50 image of an authored folder")
    iso.add_argument("source",type=Path);iso.add_argument("output",type=Path)
    iso.add_argument("--udf-tool",type=Path);iso.add_argument("--label",default="DVD2UHD")
    audit=sub.add_parser('audit',help='Compare original navigation and shared clip mapping; optionally hash every HEVC picture')
    audit.add_argument('source',type=Path);audit.add_argument('--media',action='store_true');audit.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    try:
        if args.command=='audit':
            from .audit import audit as check
            r=check(args.source,media=args.media,output=args.output)
            print(json.dumps({k:v for k,v in r.items() if k!='media'},indent=2))
        elif args.command=="iso":
            from .iso import create
            r=create(args.source,args.output,args.udf_tool,args.label)
            print(json.dumps({k:v for k,v in r.items() if k not in ('files','independent_descriptors')},indent=2))
        elif args.command=="author":
            report=author(args.source,args.output,tsmuxer=args.tsmuxer,ffmpeg=args.ffmpeg,ffprobe=args.ffprobe,
                          bdj_api=args.bdj_api,max_cells=args.max_cells,menus_only=args.menus_only)
            print(json.dumps({k:v for k,v in report.items() if k not in ('cells','joined_title_playlists')},indent=2))
        else:
            with Disc(args.source) as d:g=inspect(d)
            if args.command=="trace":
                classes=build_java(args.work/"classes",args.bdj_api)
                model(g,args.work/"disc.bin")
                print(run(["java","-cp",classes,"org.dvd2uhd.Trace",args.work/"disc.bin",*args.actions]))
            else:
                g["unsupported"]=eligibility(g)
                g["summary"]=dict(titles=len(g["titles"]),pgcs=len(g["pgcs"]),
                                  unique_cells=len(unique_cells(g)),
                                  commands=sum(len(p[n]) for p in g["pgcs"] for n in ("pre","post","cell_commands")))
                text=json.dumps(g,indent=2)
                if args.output:
                    args.output.parent.mkdir(parents=True,exist_ok=True)
                    args.output.write_text(text,encoding="utf-8")
                    print(json.dumps(g["summary"]));print("Unsupported:",g["unsupported"])
                else:print(text)
    except (OSError,ValueError,RuntimeError) as e:
        parser.exit(1,str(e)+"\n")

if __name__=="__main__":main()
