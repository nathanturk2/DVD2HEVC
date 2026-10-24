// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;
import java.awt.event.*;
import java.io.*;
import java.lang.reflect.*;
import java.util.Arrays;
import java.util.zip.GZIPOutputStream;
import org.dvb.event.UserEvent;

/** Voyager's nine-language directional links, mixed pointer/key input and SPU shape. */
public final class LanguageInputTest {
    private static void check(boolean value,String message){if(!value)throw new AssertionError(message);}
    private static void set(DvdXlet x,String name,Object value)throws Exception {
        Field f=DvdXlet.class.getDeclaredField(name);f.setAccessible(true);f.set(x,value);
    }
    private static MouseEvent move(DvdXlet x,int px,int py) {
        return new MouseEvent(x,MouseEvent.MOUSE_MOVED,0,0,px,py,0,false);
    }
    private static void key(DvdXlet x,int code) {
        x.userEventReceived(new UserEvent(x,UserEvent.UEF_KEY_EVENT,KeyEvent.KEY_PRESSED,code,0,0));
    }
    public static void main(String[] args)throws Exception {
        DiscModel disc=new DiscModel(new ByteArrayInputStream(new byte[]{68,86,68,74,0,0,0,3,0,0,0,0,0,0,0,0}));
        Navigator nav=new Navigator(disc);nav.pgc=new DiscModel.Pgc();nav.pgc.key="M:1:en:1";nav.pgc.vts=1;
        nav.pgc.uops=0x1fdffef; // Source prohibits other operations but permits menu input.
        DvdXlet x=new DvdXlet();set(x,"nav",nav);set(x,"serial",nav.serial);
        ByteArrayOutputStream raw=new ByteArrayOutputStream();DataOutputStream d=new DataOutputStream(new GZIPOutputStream(raw));
        d.writeInt(0x47584633);for(int i=0;i<4;i++)d.writeInt(0);d.close();
        set(x,"graphics",new DiscModel.Graphics(new ByteArrayInputStream(raw.toByteArray())));
        DiscModel.Menu menu=new DiscModel.Menu();menu.count=9;menu.groups=1;menu.types=new int[]{0};menu.selectEnd=Long.MAX_VALUE;menu.buttons=new DiscModel.Button[36];
        int[][] links={{1,4,9,2},{7,5,1,3},{8,6,2,3},{1,7,4,5},{2,8,4,6},{3,9,5,6},{4,2,7,8},{5,3,7,9},{6,9,8,1}};
        int[] pgcs={6,10,8,4,3,7,2,9,5};
        for(int i=0;i<9;i++) {
            DiscModel.Button b=new DiscModel.Button();b.x=100+(i%3)*180;b.x2=b.x+160;b.y=200+(i/3)*60;b.y2=b.y+55;
            b.up=links[i][0];b.down=links[i][1];b.left=links[i][2];b.right=links[i][3];b.command=0x2004000000000000L|pgcs[i];menu.buttons[i]=b;
            DiscModel.Pgc dest=new DiscModel.Pgc();dest.key="M:1:en:"+pgcs[i];dest.vts=1;dest.pre=new long[0];dest.programs=new int[]{1};dest.cells=new DiscModel.Cell[]{new DiscModel.Cell()};disc.pgcs.put(dest.key,dest);
        }
        set(x,"menu",menu);set(x,"selected",7);nav.vm.sprm[8]=7<<10;
        key(x,KeyEvent.VK_RIGHT);check(nav.vm.sprm[8]==8<<10,"remote right selects Italiano");
        key(x,KeyEvent.VK_UP);check(nav.vm.sprm[8]==5<<10,"remote up selects Francais");
        key(x,KeyEvent.VK_LEFT);key(x,KeyEvent.VK_DOWN);check(nav.vm.sprm[8]==7<<10,"directional links return to English");
        x.mouseMoved(move(x,500,350));check(nav.vm.sprm[8]==9<<10 && nav.serial==0,"hover selects Svenska without activating");
        key(x,KeyEvent.VK_LEFT);check(nav.vm.sprm[8]==8<<10,"keyboard takes over from mouse");
        for(int i=0;i<200;i++)x.mouseMoved(move(x,500,350));
        check(nav.vm.sprm[8]==8<<10,"stationary pointer refreshes cannot undo keyboard selection");
        x.mouseMoved(move(x,0,0));x.mouseMoved(move(x,500,350));check(nav.vm.sprm[8]==9<<10,"real pointer motion takes over again");
        key(x,KeyEvent.VK_ENTER);check(nav.pgc.key.equals("M:1:en:5") && nav.serial==1,"Enter executes source language command once");
        x.mousePressed(new MouseEvent(x,MouseEvent.MOUSE_PRESSED,0,0,120,220,1,false,MouseEvent.BUTTON1));
        check(nav.serial==1,"stale language screen rejects further input");
        nav.pgc.palette=new int[]{0x108080,0xeb8080,0x51f05a,0x296ef0};
        DiscModel.Picture p=new DiscModel.Picture();p.x=10;p.y=20;p.w=4;p.h=2;p.packed=new byte[]{0,1,2,3,0,1,2,3};
        p.colours=new int[]{0,1,2,3};p.alpha=new int[]{0,15,15,15};p.colcon=new int[][]{{12,20,12,20,0x01000f00}};
        DiscModel.Button highlight=new DiscModel.Button();highlight.x=highlight.x2=11;highlight.y=highlight.y2=21;
        Method render=DvdXlet.class.getDeclaredMethod("picturePixels",DiscModel.Picture.class,DiscModel.Button.class,int.class);render.setAccessible(true);
        int[] pixels=(int[])render.invoke(x,p,highlight,0x002000f0);
        check(Arrays.equals(pixels,new int[]{0,0xffffffff,0xffffffff,0xff0000ff,0,0xffff0000,0xffff0000,0xff0000ff}),"bulk rendering preserves original transparent mask, regional colours and highlight boundary");
        Method colour=DvdXlet.class.getDeclaredMethod("argb",int.class,int.class);colour.setAccessible(true);
        check((Integer)colour.invoke(x,0xeb8080,0)==0,"fully transparent pixels carry no hidden RGB into native overlay compositing");
        nav.pgc.sub=new int[32];p.start=0;p.end=1000;p.forced=true;
        DiscModel.Graphics graphics=new DiscModel.Graphics(new ByteArrayInputStream(raw.toByteArray()));graphics.pictures=new DiscModel.Picture[]{p};set(x,"graphics",graphics);
        Field timeline=DvdXlet.class.getDeclaredField("subtitles");timeline.setAccessible(true);((SubpictureTimeline)timeline.get(x)).enter(graphics,0,false);
        set(x,"actionUntil",0L);
        Method redraw=DvdXlet.class.getDeclaredMethod("repaintIfChanged",long.class);redraw.setAccessible(true);
        check((Boolean)redraw.invoke(x,0L),"first picture is drawn");
        for(int i=1;i<25;i++)check(!(Boolean)redraw.invoke(x,i*40L),"unchanged still does not redraw at video frame rate");
        set(x,"selected",8);check((Boolean)redraw.invoke(x,960L),"selection changes repaint");
        set(x,"actionUntil",System.currentTimeMillis()+10000);check((Boolean)redraw.invoke(x,960L),"activated colour repaints");
        set(x,"actionUntil",0L);check((Boolean)redraw.invoke(x,960L),"activation colour expiry repaints");
        check((Boolean)redraw.invoke(x,1001L),"subtitle end clears the picture at its original time");
        System.out.println("BD-J language input and rendering tests passed");
    }
}
