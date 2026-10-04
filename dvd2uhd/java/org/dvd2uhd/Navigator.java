// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;

import java.util.*;

/** DVD domain/PGC/program/cell navigation with bounded command-only transitions. */
public final class Navigator {
    public final DvdVm vm;
    public final DiscModel disc;
    public DiscModel.Pgc pgc;
    public int cell=1,program=1;
    public long serial=0,resumeMillis=0,currentMillis=0;
    // Only normal end-of-cell playback carries subpicture state forward.
    public boolean continuation=false,mediaSynchronized=false;
    public boolean stopped=false;
    private DiscModel.Pgc resumePgc;
    private int resumeCell;
    private int[] resumeSprm;
    private long savedResumeMillis;
    private int transitions;

    public Navigator(DiscModel disc) {this(disc,new DvdVm());}
    public Navigator(DiscModel disc,DvdVm vm) {this.disc=disc;this.vm=vm;}
    private DiscModel.Pgc find(String key) {
        DiscModel.Pgc p=disc.pgcs.get(key);
        if(p==null) throw new IllegalStateException("DVD PGC missing: "+key);
        return p;
    }
    private DiscModel.Pgc numbered(int n) {
        return find(pgc.key.substring(0,pgc.key.lastIndexOf(':')+1)+n);
    }
    private DiscModel.Title title(int n) {
        for(DiscModel.Title t:disc.titles) if(t.number==n) return t;
        throw new IllegalStateException("DVD title missing: "+n);
    }
    private DiscModel.Title localTitle(int vts,int n) {
        for(DiscModel.Title t:disc.titles) if(t.vts==vts && t.local==n) return t;
        throw new IllegalStateException("DVD VTS title missing: "+vts+":"+n);
    }
    private DiscModel.Pgc menu(int vts,int kind) {
        String lang=""+(char)(vm.sprm[0]>>8)+(char)(vm.sprm[0]&255);
        DiscModel.Pgc fallback=null;
        for(DiscModel.Pgc p:disc.pgcs.values()) {
            if(p.key.startsWith("M:"+vts+":") && p.entry==(128|kind)) {
                if(p.key.startsWith("M:"+vts+":"+lang+":")) return p;
                if(fallback==null) fallback=p;
            }
        }
        if(fallback==null) throw new IllegalStateException("DVD menu missing: "+vts+":"+kind);
        return fallback;
    }
    private DiscModel.Pgc menuNumber(int vts,int n) {
        String lang=""+(char)(vm.sprm[0]>>8)+(char)(vm.sprm[0]&255);
        DiscModel.Pgc p=disc.pgcs.get("M:"+vts+":"+lang+":"+n);
        if(p!=null) return p;
        for(DiscModel.Pgc q:disc.pgcs.values()) if(q.key.startsWith("M:"+vts+":") && q.number()==n) return q;
        throw new IllegalStateException("DVD menu PGC missing");
    }
    public void start() {
        transitions=0;
        if(disc.pgcs.containsKey("F:0:1")) enter(find("F:0:1"),1,true);
        else enter(menu(0,2),1,true);
    }
    private void enter(DiscModel.Pgc p,int pg,boolean pre) {
        if(++transitions>1000) throw new IllegalStateException("DVD navigation transition loop");
        stopped=false;pgc=p;program=pg;vm.titleDomain(p.title);vm.sprm[6]=p.title?p.number():vm.sprm[6];
        if(p.mode!=0) throw new IllegalStateException("DVD random/shuffle PGC playback is unsupported");
        if(pre) {
            DvdVm.Action a=vm.run(p.pre);
            if(a!=null && a.op!=0) { apply(a);return; }
        }
        if(p.cells.length==0) { post();return; }
        playProgram(program);
    }
    private void playProgram(int n) {
        if(n<1 || n>pgc.programs.length) throw new IllegalStateException("Invalid DVD program "+n);
        program=n;playCell(pgc.programs[n-1]);
    }
    private void playCell(int n) {
        continuation=false;mediaSynchronized=false;resumeMillis=0;
        if(n>pgc.cells.length) { post();return; }
        if(n<1) throw new IllegalStateException("Invalid DVD cell "+n);
        cell=n;
        DiscModel.Cell c=pgc.cells[n-1];
        if((c.flags&0x34)!=0) throw new IllegalStateException("DVD interleaved/angle cells require authoring support");
        for(int i=0;i<pgc.programs.length;i++) if(pgc.programs[i]<=n) program=i+1;
        if(pgc.title) {
            for(DiscModel.Title t:disc.titles) if(t.number==vm.sprm[4]) {
                for(int i=0;i<t.pgc.length;i++) if(t.pgc[i]==pgc.number() && t.program[i]<=program) vm.sprm[7]=i+1;
            }
        }
        serial++;
    }
    public DiscModel.Cell currentCell() {return stopped||pgc==null||pgc.cells.length==0?null:pgc.cells[cell-1];}
    private void post() {
        DvdVm.Action a=vm.run(pgc.post);
        if(a!=null && a.op!=0) {apply(a);return;}
        if(pgc.next!=0) enter(numbered(pgc.next),1,true);
        else {stopped=true;serial++;}
    }
    public void endCell() {
        transitions=0;
        int cmd=currentCell().command;
        if(cmd!=0) {
            if(cmd>pgc.commands.length) throw new IllegalStateException("DVD cell command missing");
            DvdVm.Action a=vm.run(new long[]{pgc.commands[cmd-1]});
            if(a!=null && a.op!=0) {apply(a);return;}
        }
        DiscModel.Pgc old=pgc;int previous=cell;
        playCell(cell+1);
        continuation=!stopped && pgc==old && cell==previous+1;
    }
    /** Adopt a native seek in a joined playlist without executing skipped commands.
     * Joined runs have no stills; a cell-end command can only finish a run.
     * Return false outside this PGC, so menu/VM transitions retain DVD semantics.
     */
    public boolean synchronizeMedia(int playlist,long millis) {
        if(stopped || pgc==null || !pgc.title)return false;
        for(int i=0;i<pgc.cells.length;i++) {
            DiscModel.Cell c=pgc.cells[i];
            if(c.playlist==playlist && c.end>c.start && millis>=c.start && millis<c.end) {
                if(cell==i+1)return false;
                playCell(i+1);currentMillis=millis-c.start;mediaSynchronized=true;
                return true;
            }
        }
        return false;
    }
    public void tick() {
        if(stopped)return;
        int target=vm.navigationTimerTarget();
        if(target!=0 && pgc!=null && pgc.title) {
            transitions=0;
            enter(find("T:"+pgc.vts+":"+target),1,true);
        }
    }
    public void button(long command,int number) {
        transitions=0;vm.sprm[8]=number<<10;
        DvdVm.Action a=vm.run(new long[]{command});
        if(a!=null) apply(a);
    }
    private void setTitle(DiscModel.Title t,int part,boolean pre) {
        if(part<1 || part>t.pgc.length) throw new IllegalStateException("Invalid DVD chapter "+part);
        vm.sprm[4]=t.number;vm.sprm[5]=t.local;vm.sprm[7]=part;vm.clearTimer();
        enter(find("T:"+t.vts+":"+t.pgc[part-1]),t.program[part-1],pre);
    }
    public void selectTitle(int n) {transitions=0;setTitle(title(n),1,true);}
    public void debugRoot(int vts) {transitions=0;enter(menu(vts,3),1,true);}
    public void chapter(int delta) {
        if(!pgc.title) return;
        transitions=0;DiscModel.Title t=title(vm.sprm[4]);
        setTitle(t,Math.max(1,Math.min(t.pgc.length,vm.sprm[7]+delta)),false);
    }
    public void rootMenu(long millis) {
        transitions=0;
        if(pgc.title) {currentMillis=millis;saveResume(0);}
        enter(menu(pgc.vts,3),1,true);
    }
    /** DVD menu Escape resumes the saved title without rerunning pre-commands. */
    public boolean resumeTitle() {
        if(resumePgc==null)return false;
        transitions=0;apply(new DvdVm.Action(16,0,0,0));return true;
    }
    private void saveResume(int n) {
        resumePgc=pgc;resumeCell=n==0?cell:n;savedResumeMillis=n==0?currentMillis:0;resumeMillis=0;
        resumeSprm=new int[5];System.arraycopy(vm.sprm,4,resumeSprm,0,5);
    }
    private void highlight(int n) {if(n!=0)vm.sprm[8]=n<<10;}
    private void apply(DvdVm.Action a) {
        int n=a.a;
        switch(a.op) {
            case 0:highlight(n);break;
            case 1:highlight(n);playCell(cell);break;
            case 2:highlight(n);playCell(cell+1);break;
            case 3:highlight(n);playCell(cell-1);break;
            case 5:highlight(n);playProgram(program);break;
            case 6:highlight(n);playProgram(program+1);break;
            case 7:highlight(n);playProgram(program-1);break;
            case 9:highlight(n);enter(pgc,1,true);break;
            case 10:highlight(n);enter(numbered(pgc.next),1,true);break;
            case 11:highlight(n);enter(numbered(pgc.prev),1,true);break;
            case 12:highlight(n);enter(numbered(pgc.up),1,true);break;
            case 13:highlight(n);post();break;
            case 16:
                if(resumePgc==null) throw new IllegalStateException("DVD resume has no saved title");
                pgc=resumePgc;vm.titleDomain(pgc.title);System.arraycopy(resumeSprm,0,vm.sprm,4,5);highlight(n);playCell(resumeCell);resumeMillis=savedResumeMillis;break;
            case 17:enter(numbered(n),1,true);break;
            case 18:highlight(a.b);setTitle(title(vm.sprm[4]),n,false);break;
            case 19:highlight(a.b);playProgram(n);break;
            case 20:highlight(a.b);playCell(n);break;
            case 21:vm.clearTimer();vm.titleDomain(false);stopped=true;serial++;break;
            case 22:setTitle(title(n),1,true);break;
            case 23:setTitle(localTitle(pgc.vts,n),1,true);break;
            case 24:setTitle(localTitle(pgc.vts,n),a.b,true);break;
            case 25:enter(find("F:0:1"),1,true);break;
            case 26:enter(menu(0,n),1,true);break;
            case 27:
                int vts=n==0?pgc.vts:n;
                if(a.b!=0) {DiscModel.Title t=localTitle(vts,a.b);vm.sprm[4]=t.number;vm.sprm[5]=a.b;}
                enter(menu(vts,a.c),1,true);break;
            case 28:enter(menuNumber(0,n),1,true);break;
            case 29:saveResume(n);enter(find("F:0:1"),1,true);break;
            case 30:saveResume(a.b);enter(menu(0,n),1,true);break;
            case 31:saveResume(a.b);enter(menu(pgc.vts,n),1,true);break;
            case 32:saveResume(a.b);enter(menuNumber(0,n),1,true);break;
            default:throw new IllegalStateException("DVD navigation opcode unsupported: "+a.op);
        }
    }
}
