// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;

import java.lang.reflect.*;
import javax.tv.service.Service;
import javax.tv.service.selection.*;
import javax.tv.locator.Locator;
import org.bluray.net.BDLocator;

/** Delayed service events must finish before the Xlet starts a new playlist. */
public final class ServiceSwitchTest implements InvocationHandler {
    private int requests,title;
    private final Service service=(Service)Proxy.newProxyInstance(Service.class.getClassLoader(),new Class[]{Service.class},this);
    private final ServiceContext context=(ServiceContext)Proxy.newProxyInstance(ServiceContext.class.getClassLoader(),new Class[]{ServiceContext.class},this);
    public Object invoke(Object proxy,Method method,Object[] args)throws Throwable {
        String name=method.getName();
        if(name.equals("getService"))return service;
        if(name.equals("getLocator"))return new BDLocator(null,title,-1);
        if(name.equals("select")) {requests++;title=((BDLocator)((Locator[])args[0])[0]).getTitleNumber();}
        return null;
    }
    private static void check(boolean value,String message){if(!value)throw new AssertionError(message);}
    private static void set(DvdXlet x,String name,Object value)throws Exception {
        Field field=DvdXlet.class.getDeclaredField(name);field.setAccessible(true);field.set(x,value);
    }
    private static boolean select(DvdXlet x,int service)throws Exception {
        Method method=DvdXlet.class.getDeclaredMethod("selectService",int.class);method.setAccessible(true);
        try{return ((Boolean)method.invoke(x,service)).booleanValue();}
        catch(InvocationTargetException error){throw (Exception)error.getCause();}
    }
    private static DiscModel.Pgc pgc(String key,boolean title) {
        DiscModel.Pgc p=new DiscModel.Pgc();p.key=key;p.title=title;p.vts=1;
        p.entry=title?0:131;p.programs=new int[]{1};p.pre=p.post=p.commands=new long[0];
        p.cells=new DiscModel.Cell[]{new DiscModel.Cell()};return p;
    }
    private static void external(DvdXlet x,ServiceSwitchTest fake,int service)throws Exception {
        fake.title=service;x.receiveServiceContextEvent(new NormalContentEvent(fake.context));
        Method method=DvdXlet.class.getDeclaredMethod("externalService");method.setAccessible(true);method.invoke(x);
    }
    public static void main(String[] args)throws Exception {
        ServiceSwitchTest fake=new ServiceSwitchTest();DvdXlet x=new DvdXlet();set(x,"titleContext",fake.context);
        check(!select(x,0) && fake.requests==1,"selection yields before loading a playlist");
        check(!select(x,0) && fake.requests==1,"pending selection is not repeated");
        ServiceSwitchTest unrelated=new ServiceSwitchTest();
        x.receiveServiceContextEvent(new NormalContentEvent(unrelated.context));
        check(!select(x,0),"another context cannot complete our request");
        x.receiveServiceContextEvent(new NormalContentEvent(fake.context));
        check(select(x,0),"menu playlist starts after completion");
        check(!select(x,1) && fake.requests==2,"movie switch starts a new asynchronous selection");
        check(!select(x,0) && fake.requests==2,"rapid return does not race an outstanding selection");
        x.receiveServiceContextEvent(new NormalContentEvent(fake.context));
        check(!select(x,0) && fake.requests==3,"return to menu waits for its own completion");
        x.receiveServiceContextEvent(new NormalContentEvent(fake.context));
        check(select(x,0),"menu state is established before its new media starts");
        check(!select(x,1),"failure case requests movie");
        x.receiveServiceContextEvent(new SelectionFailedEvent(fake.context,SelectionFailedEvent.OTHER));
        try {select(x,1);throw new AssertionError("selection failure was ignored");}
        catch(java.io.IOException expected){}
        fake=new ServiceSwitchTest();x=new DvdXlet();set(x,"titleContext",fake.context);select(x,0);
        set(x,"serviceDeadline",Long.valueOf(System.nanoTime()-1));
        try {select(x,0);throw new AssertionError("selection timeout was ignored");}
        catch(java.io.IOException expected){}
        fake=new ServiceSwitchTest();x=new DvdXlet();set(x,"titleContext",fake.context);
        DiscModel disc=new DiscModel(new java.io.ByteArrayInputStream(new byte[]{68,86,68,74,0,0,0,3,0,0,0,0,0,0,0,0}));
        DiscModel.Pgc movie=pgc("T:1:1",true),menu=pgc("M:1:en:1",false);
        disc.pgcs.put(movie.key,movie);disc.pgcs.put(menu.key,menu);
        Navigator nav=new Navigator(disc);nav.pgc=movie;nav.currentMillis=12345;set(x,"nav",nav);
        external(x,fake,0);check(nav.pgc==menu,"native Disc Menu enters the DVD menu domain");
        menu.uops=1<<16;external(x,fake,1);
        check(nav.pgc==menu,"native title selection cannot bypass the DVD resume prohibition");
        menu.uops=0;
        external(x,fake,1);check(nav.pgc==movie && nav.resumeMillis==12345,"native movie service resumes the saved cell and time");
        movie.uops=1<<11;nav.currentMillis=23456;long serial=nav.serial;
        external(x,fake,0);check(nav.pgc==movie && nav.resumeMillis==23456 && nav.serial>serial,"forbidden menu restores the movie service and offset");
        movie.uops=0;disc.pgcs.remove(menu.key);nav.currentMillis=34567;
        external(x,fake,0);check(nav.pgc==movie && nav.resumeMillis==34567,"absent menu leaves movie playback intact");
        System.out.println("BD-J service selection tests passed");
    }
}
