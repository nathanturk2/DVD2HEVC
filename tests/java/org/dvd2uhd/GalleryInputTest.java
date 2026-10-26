// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;
import java.io.*;
import java.lang.reflect.*;
import javax.media.*;

public final class GalleryInputTest implements InvocationHandler {
    private int stops;
    public Object invoke(Object proxy,Method method,Object[] args) {
        if(method.getName().equals("stop"))stops++;
        if(method.getName().equals("getState"))return Integer.valueOf(Controller.Started);
        if(method.getName().equals("getMediaNanoseconds"))return Long.valueOf(11000000000L);
        return null;
    }
    private static void check(boolean b,String s){if(!b)throw new AssertionError(s);}
    private static void set(DvdXlet x,String name,Object value)throws Exception {
        Field f=DvdXlet.class.getDeclaredField(name);f.setAccessible(true);f.set(x,value);
    }
    private static DiscModel.Pgc pgc(int n,int count) {
        DiscModel.Pgc p=new DiscModel.Pgc();p.key="T:1:"+n;p.vts=1;p.title=true;
        p.programs=new int[count];p.cells=new DiscModel.Cell[count];
        p.pre=new long[]{0x7100000000630000L};p.post=new long[0];p.commands=new long[]{0x7100000100630000L};
        for(int i=0;i<count;i++) {
            p.programs[i]=i+1;DiscModel.Cell c=new DiscModel.Cell();p.cells[i]=c;
            c.playlist=8;c.nativeItem=n==1?i*2:4;c.start=c.nativeItem*20;c.end=c.start+20;
            c.duration=c.presentation=480;c.still=255;
        }
        return p;
    }
    public static void main(String[] args)throws Exception {
        DiscModel disc=new DiscModel(new ByteArrayInputStream(new byte[]{68,86,68,74,0,0,0,3,0,0,0,0,0,0,0,0}));
        DiscModel.Pgc a=pgc(1,2),b=pgc(2,1);disc.pgcs.put(a.key,a);disc.pgcs.put(b.key,b);
        DiscModel.Title title=new DiscModel.Title();title.number=title.vts=title.local=1;
        title.pgc=new int[]{1,1,2};title.program=new int[]{1,2,1};disc.titles.add(title);
        Navigator nav=new Navigator(disc);nav.selectTitle(1);nav.vm.set(0,0);a.cells[0].command=1;
        check(!nav.synchronizeGallery(8,1,20),"unmarked guard is not a DVD chapter");
        check(nav.synchronizeGallery(8,2,40) && nav.cell==2 && nav.vm.sprm[7]==2,"next native chapter selects one still");
        check(nav.vm.get(0)==0 && nav.vm.get(1)==0,"chapter seek bypasses pre and skipped cell commands");
        check(nav.synchronizeGallery(8,4,80) && nav.pgc==b && nav.vm.sprm[7]==3,"native chapter can cross PGCs");
        check(nav.synchronizeGallery(8,0,0) && nav.pgc==a && nav.cell==1,"previous chapter can return across PGCs");
        check(!nav.synchronizeGallery(9,2,40),"unrelated playlist cannot move DVD state");
        DvdXlet x=new DvdXlet();GalleryInputTest fake=new GalleryInputTest();
        set(x,"nav",nav);set(x,"player",Proxy.newProxyInstance(Player.class.getClassLoader(),new Class[]{Player.class},fake));
        set(x,"loadedPlaylist",8);
        Method sync=DvdXlet.class.getDeclaredMethod("synchronizeGallery",int.class,int.class,long.class);sync.setAccessible(true);
        check(!(Boolean)sync.invoke(x,8,4,80L) && nav.pgc==a,"stale previous player item cannot undo a DVD command");
        check(!(Boolean)sync.invoke(x,9,0,0L),"another playlist cannot arm gallery seeks");
        check(!(Boolean)sync.invoke(x,8,2,40L),"unrequested item is ignored until locator is ready");
        check(!(Boolean)sync.invoke(x,8,0,0L),"requested item arms native chapter tracking");
        check((Boolean)sync.invoke(x,8,2,40L) && nav.cell==2,"subsequent native chapter seek is accepted");
        nav.synchronizeGallery(8,0,0);
        Method step=DvdXlet.class.getDeclaredMethod("advancePresentation",DiscModel.Cell.class,long.class,long.class,long.class);step.setAccessible(true);
        step.invoke(x,a.cells[0],10000L,480L,10000L);
        check(nav.cell==1 && fake.stops==0,"held gallery cannot auto-advance at native item EOF");
        Field ended=DvdXlet.class.getDeclaredField("ended");ended.setAccessible(true);
        check((Boolean)ended.get(x),"joined native still still enters the DVD wait");
        System.out.println("BD-J gallery chapter tests passed");
    }
}
