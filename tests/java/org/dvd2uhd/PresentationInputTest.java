// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;
import java.io.*;
import java.lang.reflect.*;
import javax.media.*;

/** Buffered video must finish through the native decoder's completion event. */
public final class PresentationInputTest implements InvocationHandler {
    private int stops;
    public Object invoke(Object proxy,Method method,Object[] args) {
        if(method.getName().equals("stop"))stops++;
        if(method.getName().equals("getMediaNanoseconds"))return Long.valueOf(17530000000L);
        return null;
    }
    private static void set(DvdXlet x,String name,Object value)throws Exception {
        Field f=DvdXlet.class.getDeclaredField(name);f.setAccessible(true);f.set(x,value);
    }
    private static Object get(DvdXlet x,String name)throws Exception {
        Field f=DvdXlet.class.getDeclaredField(name);f.setAccessible(true);return f.get(x);
    }
    private static void check(boolean value,String message){if(!value)throw new AssertionError(message);}
    public static void main(String[] args)throws Exception {
        DiscModel disc=new DiscModel(new ByteArrayInputStream(new byte[]{68,86,68,74,0,0,0,3,0,0,0,0,0,0,0,0}));
        Navigator nav=new Navigator(disc);nav.pgc=new DiscModel.Pgc();
        DiscModel.Cell c=new DiscModel.Cell(),tail=new DiscModel.Cell();
        c.presentation=c.duration=11000;c.playlist=19;tail.playlist=20;
        nav.pgc.cells=new DiscModel.Cell[]{c,tail};nav.pgc.programs=new int[]{1};
        DvdXlet x=new DvdXlet();PresentationInputTest fake=new PresentationInputTest();
        Player player=(Player)Proxy.newProxyInstance(Player.class.getClassLoader(),new Class[]{Player.class},fake);
        set(x,"nav",nav);set(x,"player",player);
        Method step=DvdXlet.class.getDeclaredMethod("advancePresentation",DiscModel.Cell.class,long.class,long.class,long.class);step.setAccessible(true);
        step.invoke(x,c,11040L,11000L,13000L);
        check(fake.stops==0 && !(Boolean)get(x,"ended") && nav.cell==1,"IFO timer must not discard buffered ordinary video");
        x.controllerUpdate(new EndOfMediaEvent(player,Controller.Started,Controller.Prefetched,Controller.Prefetched,new Time(17.53)));
        check((Boolean)get(x,"ended"),"native completion releases ordinary video");
        set(x,"ended",false);c.end=11000;
        step.invoke(x,c,11040L,11000L,13000L);
        check(nav.cell==1 && nav.serial==0 && fake.stops==0,"last joined item must drain before a different playlist");
        tail.playlist=19;tail.start=11000;tail.end=12000;
        step.invoke(x,c,11040L,11000L,13000L);
        check(nav.cell==2 && nav.continuation && fake.stops==0,"adjacent joined cells continue without closing the decoder");
        nav.cell=1;c.end=0;c.repeated=true;
        step.invoke(x,c,11040L,11000L,13000L);
        check((Boolean)get(x,"ended") && fake.stops==0,"repeated stills preserve DVD timing and native keepalive");
        set(x,"ended",false);c.repeated=false;c.still=255;
        step.invoke(x,c,11040L,11000L,13000L);
        check((Boolean)get(x,"ended") && fake.stops==0,"native-held stills keep their DVD wait");
        System.out.println("BD-J presentation completion tests passed");
    }
}
