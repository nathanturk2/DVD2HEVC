// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;

import java.awt.event.*;
import java.io.*;
import java.lang.reflect.*;
import java.util.zip.GZIPOutputStream;
import org.dvb.event.UserEvent;

/** Exercise the actual Xlet event handlers against libdvdnav's mouse contract. */
public final class MouseInputTest {
    private static void check(boolean value,String message) {
        if(!value)throw new AssertionError(message);
    }
    private static void set(DvdXlet x,String name,Object value)throws Exception {
        Field f=DvdXlet.class.getDeclaredField(name);f.setAccessible(true);f.set(x,value);
    }
    private static MouseEvent event(DvdXlet x,int type,int px,int py) {
        return new MouseEvent(x,type,0,0,px,py,1,false,MouseEvent.BUTTON1);
    }
    private static DiscModel.Button button(int left,int right,int auto) {
        DiscModel.Button b=new DiscModel.Button();b.x=left;b.x2=right;b.y=10;b.y2=30;b.auto=auto;
        // Increment GPRM0: detects both accidental hover activation and double execution.
        b.command=0x7300000000010000L;b.right=2;b.left=1;return b;
    }
    public static void main(String[] args)throws Exception {
        DiscModel disc=new DiscModel(new ByteArrayInputStream(new byte[]{68,86,68,74,0,0,0,3,0,0,0,0,0,0,0,0}));
        Navigator nav=new Navigator(disc);nav.pgc=new DiscModel.Pgc();
        DvdXlet x=new DvdXlet();set(x,"nav",nav);set(x,"serial",Long.valueOf(nav.serial));
        ByteArrayOutputStream raw=new ByteArrayOutputStream();
        DataOutputStream d=new DataOutputStream(new GZIPOutputStream(raw));
        d.writeInt(0x47584633);for(int i=0;i<4;i++)d.writeInt(0);d.close();
        DiscModel.Graphics graphics=new DiscModel.Graphics(new ByteArrayInputStream(raw.toByteArray()));
        set(x,"graphics",graphics);
        DiscModel.Menu menu=new DiscModel.Menu();menu.count=2;menu.groups=1;menu.types=new int[]{0};
        menu.selectEnd=Long.MAX_VALUE;menu.buttons=new DiscModel.Button[36];
        menu.buttons[0]=button(10,30,0);menu.buttons[1]=button(50,70,1);
        set(x,"menu",menu);set(x,"selected",Integer.valueOf(1));nav.vm.sprm[8]=1<<10;
        for(int i=0;i<100;i++)x.mouseMoved(event(x,MouseEvent.MOUSE_MOVED,60,20));
        check(nav.vm.sprm[8]==2<<10 && nav.vm.get(0)==0,"auto-action button hover selects without execution");
        x.mouseDragged(event(x,MouseEvent.MOUSE_DRAGGED,61,21));
        check(nav.vm.get(0)==0,"dragging does not activate");
        x.mousePressed(event(x,MouseEvent.MOUSE_PRESSED,60,20));
        x.mouseReleased(event(x,MouseEvent.MOUSE_RELEASED,60,20));
        x.mouseClicked(event(x,MouseEvent.MOUSE_CLICKED,60,20));
        check(nav.vm.get(0)==1,"one auto-action click executes once across the AWT event sequence");
        x.mousePressed(event(x,MouseEvent.MOUSE_PRESSED,20,20));
        check(nav.vm.get(0)==2 && nav.vm.sprm[8]==1<<10,"click selects a different ordinary button and executes once");
        x.mouseMoved(event(x,MouseEvent.MOUSE_MOVED,90,20));
        x.mousePressed(event(x,MouseEvent.MOUSE_PRESSED,90,20));
        check(nav.vm.get(0)==2 && nav.vm.sprm[8]==1<<10,"outside retains highlight without activation");
        menu.buttons[0]=button(10,70,0);menu.buttons[1]=button(50,70,1);
        x.mouseExited(event(x,MouseEvent.MOUSE_EXITED,90,20));
        x.mouseMoved(event(x,MouseEvent.MOUSE_MOVED,60,20));
        check(nav.vm.sprm[8]==2<<10,"overlap chooses nearest centre rather than first rectangle");
        menu.buttons[1]=button(10,70,1);
        x.mouseExited(event(x,MouseEvent.MOUSE_EXITED,90,20));
        x.mouseMoved(event(x,MouseEvent.MOUSE_MOVED,60,20));
        check(nav.vm.sprm[8]==1<<10,"equal distance chooses first button");
        x.mouseMoved(event(x,MouseEvent.MOUSE_MOVED,10,10));
        check(nav.vm.sprm[8]==1<<10,"rectangle edges are inclusive");
        menu.buttons[0]=button(10,30,0);menu.buttons[1]=button(50,70,1);
        x.userEventReceived(new UserEvent(x,UserEvent.UEF_KEY_EVENT,KeyEvent.KEY_PRESSED,KeyEvent.VK_RIGHT,0,0));
        check(nav.vm.get(0)==3 && nav.vm.sprm[8]==2<<10,"arrow-key selection retains DVD auto-action");
        nav.pgc.uops=1<<17;
        x.mousePressed(event(x,MouseEvent.MOUSE_PRESSED,20,20));
        check(nav.vm.get(0)==3 && nav.vm.sprm[8]==2<<10,"DVD button prohibition blocks mouse");
        nav.pgc.uops=0;menu.selectEnd=0;
        x.mousePressed(event(x,MouseEvent.MOUSE_PRESSED,20,20));
        check(nav.vm.get(0)==3,"expired selection window blocks mouse");
        menu.selectEnd=Long.MAX_VALUE;nav.serial++;
        x.mousePressed(event(x,MouseEvent.MOUSE_PRESSED,20,20));
        check(nav.vm.get(0)==3,"events queued after a DVD navigation jump cannot activate stale buttons");
        System.out.println("BD-J mouse input tests passed");
    }
}
