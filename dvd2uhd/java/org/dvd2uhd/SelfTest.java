// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;
import java.util.Random;

/** Executable register-machine assertions, without a BD-J runtime. */
public final class SelfTest {
    private static void check(boolean c,String message) {if(!c)throw new AssertionError(message);}
    private static long cmd(String hex) {return Long.parseUnsignedLong(hex,16);}
    public static void main(String[] args)throws Exception {
        DvdVm v=new DvdVm(new Random(1));
        v.execute(cmd("71000000ffff0000"));check(v.get(0)==65535,"assign");
        v.execute(cmd("7300000000010000"));check(v.get(0)==65535,"saturating add");
        v.execute(cmd("74000000ffff0000"));check(v.get(0)==0,"subtract");
        v.execute(cmd("7400000000010000"));check(v.get(0)==0,"saturating subtract");
        v.execute(cmd("71000000ffff0000"));
        v.execute(cmd("75000000ffff0000"));check(v.get(0)==65535,"multiply without overflow");
        v.execute(cmd("7600000000000000"));check(v.get(0)==65535,"divide by zero");
        v.execute(cmd("7700000000000000"));check(v.get(0)==65535,"modulo by zero");
        v.execute(cmd("7900000000ff0000"));check(v.get(0)==255,"and");
        v.execute(cmd("7a000000ff000000"));check(v.get(0)==65535,"or");
        v.execute(cmd("7b00000000ff0000"));check(v.get(0)==65280,"xor");
        v.execute(cmd("78000000000a0000"));check(v.get(0)>=1 && v.get(0)<=10,"random bounds");
        DvdVm.Action a=v.execute(cmd("3002000000010000"));check(a.op==22 && a.a==1,"JumpTT");
        a=v.execute(cmd("3005000300010000"));check(a.op==24 && a.a==1 && a.b==3,"JumpVTS_PTT");
        v.execute(cmd("51000000c2000000"));check(v.sprm[2]==66,"SetSTN immediate");
        v.execute(cmd("5600000038000000"));check(v.sprm[8]==14336,"SetHL_BTNN");
        v.set(0,42);v.set(1,7);v.execute(cmd("6200000000010000"));
        check(v.get(0)==7 && v.get(1)==42,"swap");
        v.set(0,1);
        check(v.execute(cmd("20a4000000010002"))!=null,"conditional link equal");
        v.set(0,2);check(v.execute(cmd("20a4000000010002"))==null,"conditional link false");
        boolean loop=false;
        try {v.run(new long[]{cmd("0001000000000001")});}catch(IllegalStateException e){loop=true;}
        check(loop,"goto loop bounded");
        boolean unknown=false;
        try {v.execute(cmd("e000000000000000"));}catch(IllegalArgumentException e){unknown=true;}
        check(unknown,"reserved opcode rejected");
        final long[] nanos={0};PlaybackClock clock=new PlaybackClock(new PlaybackClock.Source(){public long nanos(){return nanos[0];}});
        DvdVm timed=new DvdVm(new Random(1),clock);
        timed.execute(cmd("5300000200800000"));
        nanos[0]=1500000000L;check(timed.get(0)==3,"GPRM counter elapsed");
        clock.pause(true);nanos[0]=9500000000L;check(timed.get(0)==3,"paused counter frozen");
        clock.pause(false);nanos[0]=11000000000L;check(timed.get(0)==5,"counter resumes without pause time");
        timed.titleDomain(false);nanos[0]=20000000000L;check(timed.get(0)==5,"GPRM frozen in DVD menu domain");
        timed.titleDomain(true);nanos[0]=21000000000L;check(timed.get(0)==6,"GPRM resumes in title domain");
        timed.execute(cmd("5200000200010000"));
        nanos[0]=22000000000L;timed.refreshTimer();check(timed.sprm[9]==1,"navigation timer counts down");
        timed.titleDomain(false);nanos[0]=30000000000L;check(timed.navigationTimerTarget()==0 && timed.sprm[9]==1,"navigation timer frozen in menu");
        timed.titleDomain(true);nanos[0]=31000000000L;check(timed.navigationTimerTarget()==1,"timer expires to target PGC");
        check(timed.navigationTimerTarget()==0,"timer expires only once");
        timed.execute(cmd("5200000500010000"));timed.execute(cmd("5200000000010000"));
        nanos[0]=40000000000L;check(timed.navigationTimerTarget()==0,"SetNVTMR zero cancels timer");
        // A compact valid empty model, populated with a synthetic joined PGC.
        DiscModel disc=new DiscModel(new java.io.ByteArrayInputStream(new byte[]{68,86,68,74,0,0,0,3,0,0,0,0,0,0,0,0}));
        DiscModel.Pgc pgc=new DiscModel.Pgc();pgc.key="T:1:1";pgc.vts=1;pgc.title=true;
        pgc.programs=new int[]{1,2,3};pgc.pre=pgc.post=pgc.commands=new long[0];pgc.cells=new DiscModel.Cell[3];
        for(int i=0;i<3;i++){DiscModel.Cell c=new DiscModel.Cell();c.playlist=4;c.start=i*1000;c.end=c.start+1000;c.duration=1000;pgc.cells[i]=c;}
        disc.pgcs.put(pgc.key,pgc);DiscModel.Title title=new DiscModel.Title();title.number=title.local=title.vts=1;
        title.pgc=new int[]{1,1,1};title.program=new int[]{1,2,3};disc.titles.add(title);
        Navigator nav=new Navigator(disc);nav.selectTitle(1);nav.endCell();
        check(nav.cell==2 && nav.continuation,"normal cell continuation");
        check(nav.synchronizeMedia(4,2500) && nav.cell==3 && nav.vm.sprm[7]==3,"forward native seek chapter");
        check(!nav.continuation && nav.mediaSynchronized,"seek is not natural continuation");
        check(nav.synchronizeMedia(4,500) && nav.cell==1 && nav.vm.sprm[7]==1,"backward native seek chapter");
        check(!nav.synchronizeMedia(99,1500) && nav.cell==1,"unrelated playlist does not change DVD state");
        DiscModel.Picture regional=new DiscModel.Picture();
        regional.colcon=new int[][]{{1,2,3,4,0x12345678},{3,2,5,4,0x2222ffff}};
        check(regional.colourWord(0,2)==-1,"outside SPU colour region");
        check(regional.colourWord(2,3)==0x12345678,"SPU regional palette and alpha");
        check(regional.colourWord(3,4)==0x2222ffff,"rightmost colour region overrides");
        regional.colcon=new int[][]{{0,0,3,4,-1}};
        check(regional.colourWord(0,0)==0xffffffffL,"all-white opaque word is not absent");
        nav.button(cmd("3001000000000000"),1);check(nav.stopped,"Exit stops");
        nav.selectTitle(1);check(!nav.stopped,"title selection restarts stopped VM");
        DiscModel.Pgc root=new DiscModel.Pgc();root.key="M:1:en:1";root.vts=1;root.entry=131;
        root.programs=new int[]{1};root.cells=new DiscModel.Cell[]{pgc.cells[0]};root.pre=root.post=root.commands=new long[0];disc.pgcs.put(root.key,root);
        nav.synchronizeMedia(4,1500);nav.vm.sprm[8]=3<<10;nav.rootMenu(375);
        check(!nav.pgc.title && nav.cell==1,"root menu stores title position");
        check(nav.resumeTitle() && nav.pgc==pgc && nav.cell==2 && nav.resumeMillis==375,"menu Escape resumes exact cell offset");
        check(nav.vm.sprm[7]==2 && nav.vm.sprm[8]==3<<10,"resume restores chapter and highlight state");
        // SPU with no STOP_DISPLAY carries into the next cell, then a clear
        // packet ends it. Seeking never carries a picture into a new cell.
        // Construct empty GXF3 through the same public reader as real discs.
        java.io.ByteArrayOutputStream bytes=new java.io.ByteArrayOutputStream();
        java.util.zip.GZIPOutputStream zipped=new java.util.zip.GZIPOutputStream(bytes);
        java.io.DataOutputStream data=new java.io.DataOutputStream(zipped);
        data.writeInt(0x47584633);for(int i=0;i<4;i++)data.writeInt(0);data.close();
        DiscModel.Graphics first=new DiscModel.Graphics(new java.io.ByteArrayInputStream(bytes.toByteArray()));
        DiscModel.Graphics next=new DiscModel.Graphics(new java.io.ByteArrayInputStream(bytes.toByteArray()));
        DiscModel.Picture picture=new DiscModel.Picture();picture.stream=0;picture.start=500;picture.end=5000;
        first.pictures=new DiscModel.Picture[]{picture};
        SubpictureTimeline timeline=new SubpictureTimeline();timeline.enter(first,0,false);
        timeline.enter(next,1000,true);check(timeline.active(0,500).picture==picture,"subtitle carries across cell");
        next.resetStreams=new int[]{0};next.resetTimes=new long[]{700};
        check(timeline.active(0,699)!=null && timeline.active(0,700)==null,"invisible packet clears carried subtitle");
        timeline.enter(first,0,false);timeline.enter(next,1000,false);check(timeline.active(0,100)==null,"seek discards subtitle carry");
        System.out.println("VM self-tests passed");
    }
}
