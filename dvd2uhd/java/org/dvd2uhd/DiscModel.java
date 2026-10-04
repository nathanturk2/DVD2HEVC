// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;

import java.io.*;
import java.util.*;
import java.util.zip.GZIPInputStream;
import java.util.zip.InflaterInputStream;

public final class DiscModel {
    public static final class Cell {
        public int flags,still,command,playlist;
        public long duration,start,end,presentation;
        public boolean repeated,keepalive,nativeSubtitles;
        public String graphics;
        public int[] audioStreams;
    }
    public static final class Pgc {
        public String key;
        public int vts,entry,next,prev,up,still,mode,width,height;
        public boolean wide,title;
        public int uops;
        public int[] audio,sub,programs,palette;
        public long[] pre,post,commands;
        public Cell[] cells;
        public int number() { return Integer.parseInt(key.substring(key.lastIndexOf(':')+1)); }
    }
    public static final class Title {
        public int number,vts,local;
        public int[] pgc,program;
    }
    public final Map<String,Pgc> pgcs=new LinkedHashMap<String,Pgc>();
    public final List<Title> titles=new ArrayList<Title>();
    private static int count(DataInputStream d,int max) throws IOException {
        int n=d.readInt();
        if(n<0 || n>max) throw new IOException("Invalid disc data count "+n);
        return n;
    }
    private static int[] ints(DataInputStream d,int max) throws IOException {
        int[] a=new int[count(d,max)];
        for(int i=0;i<a.length;i++) a[i]=d.readInt();
        return a;
    }
    private static long[] commands(DataInputStream d) throws IOException {
        long[] a=new long[count(d,255)];
        for(int i=0;i<a.length;i++) a[i]=d.readLong();
        return a;
    }
    public DiscModel(InputStream source) throws IOException {
        DataInputStream d=new DataInputStream(new BufferedInputStream(source));
        try {
            if(d.readInt()!=0x4456444a)throw new IOException("Unsupported DVDJ model");
            int version=d.readInt();if(version<3 || version>6)throw new IOException("Unsupported DVDJ model version");
            int n=count(d,30000);
            for(int i=0;i<n;i++) {
                Pgc p=new Pgc();
                p.key=d.readUTF(); p.vts=d.readInt(); p.title=d.readBoolean();
                p.entry=d.readInt(); p.next=d.readInt(); p.prev=d.readInt();p.up=d.readInt();
                p.still=d.readInt(); p.mode=d.readInt(); p.width=d.readInt();p.height=d.readInt();p.wide=d.readBoolean();
                if(version>=4)p.uops=d.readInt();
                p.audio=ints(d,8);p.sub=ints(d,32);p.programs=ints(d,255);p.palette=ints(d,16);
                p.pre=commands(d);p.post=commands(d);p.commands=commands(d);
                p.cells=new Cell[count(d,255)];
                for(int j=0;j<p.cells.length;j++) {
                    Cell c=new Cell();c.flags=d.readInt();c.still=d.readInt();c.command=d.readInt();
                    c.duration=d.readLong();c.playlist=d.readInt();c.graphics=d.readUTF();c.audioStreams=ints(d,8);
                    c.start=d.readLong();c.end=d.readLong();
                    c.presentation=version>=5?d.readLong():c.duration;
                    int repeat=version>=5?d.readUnsignedByte():0;
                    if((repeat&~3)!=0)throw new IOException("Unknown still flags");
                    c.repeated=(repeat&1)!=0;c.keepalive=(repeat&2)!=0;
                    c.nativeSubtitles=version>=6 && d.readBoolean();
                    p.cells[j]=c;
                }
                pgcs.put(p.key,p);
            }
            n=count(d,99);
            for(int i=0;i<n;i++) {
                Title t=new Title();t.number=d.readInt();t.vts=d.readInt();t.local=d.readInt();
                t.pgc=ints(d,999);t.program=ints(d,999);titles.add(t);
            }
        } finally { d.close(); }
    }
    public static final class Button {
        public int colour,x,y,x2,y2,auto,up,down,left,right;
        public long command;
    }
    public static final class Menu {
        public long start,end,selectEnd;
        public int count,groups,select,activate;
        public int[] types,colours;
        public Button[] buttons;
        public int group(boolean wide) {
            for(int i=0;i<groups;i++) if(wide ? (types[i]&1)!=0 : types[i]==0) return i;
            return 0;
        }
        public Button button(int n,boolean wide) {
            if(n<1 || n>count) return null;
            return buttons[group(wide)*(36/Math.max(1,groups))+n-1];
        }
    }
    public static final class Picture {
        public int stream,x,y,w,h;
        public long start,end;
        public boolean forced;
        public int[] colours,alpha;
        public int[][] colcon=new int[0][];
        /** PCI highlighting is applied after these original SPU changes. */
        public long colourWord(int px,int py) {
            long word=-1;
            for(int[] r:colcon)if(px>=r[0] && py>=r[1] && px<=r[2] && py<=r[3])word=r[4]&0xffffffffL;
            return word;
        }
        public byte[] packed;
        public boolean compressed;
        public byte[] mask() throws IOException {
            if(!compressed)return packed;
            byte[] result=new byte[w*h];
            DataInputStream d=new DataInputStream(new InflaterInputStream(new ByteArrayInputStream(packed)));
            try {d.readFully(result);if(d.read()!=-1)throw new IOException("Graphics mask expands beyond picture bounds");}
            finally {d.close();}
            return result;
        }
    }
    public static final class Graphics {
        public Menu[] menus;
        public Picture[] pictures;
        public int[] resetStreams=new int[0],uopMasks=new int[0];
        public long[] resetTimes=new long[0],uopTimes=new long[0];
        public Graphics(InputStream stream) throws IOException {
            if(stream==null) throw new IOException("Graphics resource missing");
            DataInputStream d=new DataInputStream(new GZIPInputStream(stream));
            try {
                int magic=d.readInt();
                if(magic<0x47584631 || magic>0x47584634) throw new IOException("Invalid graphics data");
                menus=new Menu[count(d,100000)];
                for(int i=0;i<menus.length;i++) {
                    Menu m=new Menu();m.start=d.readLong();m.end=d.readLong();
                    m.count=d.readInt();m.groups=d.readInt();m.select=d.readInt();m.activate=d.readInt();
                    m.selectEnd=magic>=0x47584633?d.readLong():m.end;
                    m.types=ints(d,3);m.colours=ints(d,6);m.buttons=new Button[36];
                    for(int j=0;j<36;j++) {
                        Button b=new Button();b.colour=d.readInt();b.x=d.readInt();b.y=d.readInt();
                        b.x2=d.readInt();b.y2=d.readInt();b.auto=d.readInt();
                        b.up=d.readInt();b.down=d.readInt();b.left=d.readInt();b.right=d.readInt();
                        b.command=d.readLong();m.buttons[j]=b;
                    }
                    menus[i]=m;
                }
                pictures=new Picture[count(d,100000)];
                for(int i=0;i<pictures.length;i++) {
                    Picture p=new Picture();p.stream=d.readInt();p.start=d.readLong();p.end=d.readLong();
                    p.forced=d.readBoolean();p.x=d.readInt();p.y=d.readInt();p.w=d.readInt();p.h=d.readInt();
                    p.colours=ints(d,4);p.alpha=ints(d,4);
                    int size=count(d,720*576+1024);
                    if(p.w<1 || p.h<1 || p.w>720 || p.h>576)throw new IOException("Invalid graphics dimensions");
                    p.compressed=magic!=0x47584631;
                    if(!p.compressed && size!=p.w*p.h) throw new IOException("Invalid graphics mask size");
                    p.packed=new byte[size];d.readFully(p.packed);pictures[i]=p;
                    if(magic>=0x47584634) {
                        p.colcon=new int[count(d,11000)][5];
                        for(int[] r:p.colcon) {
                            for(int k=0;k<5;k++)r[k]=d.readInt();
                            if(r[0]<0 || r[1]<0 || r[2]>4095 || r[3]>4095 || r[2]<r[0] || r[3]<r[1])throw new IOException("Invalid colour-change rectangle");
                        }
                    }
                }
                if(magic>=0x47584633) {
                    int n=count(d,100000);resetStreams=new int[n];resetTimes=new long[n];
                    for(int i=0;i<n;i++) {resetStreams[i]=d.readInt();resetTimes[i]=d.readLong();if(resetStreams[i]<0 || resetStreams[i]>31)throw new IOException("Invalid SPU stream");}
                    n=count(d,100000);uopTimes=new long[n];uopMasks=new int[n];
                    for(int i=0;i<n;i++){uopTimes[i]=d.readLong();uopMasks[i]=d.readInt();}
                }
            } finally {d.close();}
        }
        public long latestReset(int stream,long time) {
            long latest=Long.MIN_VALUE;
            for(int i=0;i<resetStreams.length;i++)if(resetStreams[i]==stream && resetTimes[i]<=time)latest=Math.max(latest,resetTimes[i]);
            return latest;
        }
        public Picture picture(int stream,long time) {
            long reset=latestReset(stream,time);Picture result=null;
            for(Picture p:pictures)if(p.stream==stream && p.start>=reset && time>=p.start && time<p.end && (result==null || result.start<=p.start))result=p;
            return result;
        }
        public int uops(long time) {
            int mask=0;long latest=Long.MIN_VALUE;
            for(int i=0;i<uopTimes.length;i++)if(uopTimes[i]<=time && uopTimes[i]>=latest){mask=uopMasks[i];latest=uopTimes[i];}
            return mask;
        }
    }
}
