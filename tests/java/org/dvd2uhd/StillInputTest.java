// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;
import java.io.*;
import java.lang.reflect.*;
import javax.media.Player;

/** Native stills keep overlay input alive without pausing DVD timers. */
public final class StillInputTest implements InvocationHandler {
    private int stops;
    public Object invoke(Object proxy,Method method,Object[] args) {
        if(method.getName().equals("stop"))stops++;
        return null;
    }
    private static void set(DvdXlet x,String name,Object value)throws Exception {
        Field f=DvdXlet.class.getDeclaredField(name);f.setAccessible(true);f.set(x,value);
    }
    private static void check(boolean value,String message){if(!value)throw new AssertionError(message);}
    public static void main(String[] args)throws Exception {
        DiscModel disc=new DiscModel(new ByteArrayInputStream(new byte[]{68,86,68,74,0,0,0,3,0,0,0,0,0,0,0,0}));
        Navigator nav=new Navigator(disc);nav.pgc=new DiscModel.Pgc();
        DiscModel.Cell c=new DiscModel.Cell();c.presentation=480;nav.pgc.cells=new DiscModel.Cell[]{c,c};
        DvdXlet x=new DvdXlet();StillInputTest fake=new StillInputTest();
        set(x,"nav",nav);set(x,"player",Proxy.newProxyInstance(Player.class.getClassLoader(),new Class[]{Player.class},fake));
        Method end=DvdXlet.class.getDeclaredMethod("presentationEnded",DiscModel.Cell.class,long.class);end.setAccessible(true);
        c.still=255;end.invoke(x,c,100L);check(fake.stops==0,"infinite cell still retains the decoder");
        c.still=3;end.invoke(x,c,200L);check(fake.stops==0,"finite cell still retains the decoder");
        c.still=0;nav.pgc.still=5;nav.cell=2;end.invoke(x,c,300L);check(fake.stops==0,"last-cell PGC still retains decoder");
        nav.cell=1;end.invoke(x,c,400L);check(fake.stops==1,"PGC still applies only to its last cell");
        nav.pgc.still=0;c.repeated=true;end.invoke(x,c,500L);check(fake.stops==1,"certified repeated video retains keepalive");
        c.repeated=false;end.invoke(x,c,600L);check(fake.stops==2,"ordinary presentation still stops normally");
        Field f=DvdXlet.class.getDeclaredField("clock");f.setAccessible(true);
        check(!((PlaybackClock)f.get(x)).paused(),"holding a native picture does not pause DVD wait/counter time");
        System.out.println("BD-J native still tests passed");
    }
}
