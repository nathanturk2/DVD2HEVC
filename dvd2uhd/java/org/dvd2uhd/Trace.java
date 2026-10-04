// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;
import java.io.*;

/** Headless oracle: exercise exactly the same VM/navigation used on the disc. */
public final class Trace {
    public static void main(String[] args) throws Exception {
        if(args[0].equals("graphics")) {
            java.util.zip.ZipFile jar=new java.util.zip.ZipFile(args[1]);int resources=0,pictures=0,buttons=0,forced=0,regional=0;
            try {
                java.util.Enumeration<? extends java.util.zip.ZipEntry> entries=jar.entries();
                while(entries.hasMoreElements()) {
                    java.util.zip.ZipEntry entry=entries.nextElement();
                    if(!entry.getName().endsWith(".gfx"))continue;
                    DiscModel.Graphics g=new DiscModel.Graphics(jar.getInputStream(entry));resources++;
                    for(DiscModel.Picture p:g.pictures) {
                        byte[] mask=p.mask();
                        for(byte b:mask)if(b<0 || b>3)throw new IOException("Invalid 2-bit DVD mask");
                        pictures++;
                        if(p.forced)forced++;
                        if(p.colcon.length>0)regional++;
                    }
                    for(DiscModel.Menu m:g.menus) {
                        buttons+=m.count;
                        for(DiscModel.Button b:m.buttons)new DvdVm().execute(b.command);
                    }
                }
            } finally {jar.close();}
            System.out.println("Validated "+resources+" graphics resources, "+pictures+" pictures and "+buttons+" buttons under bounded heap; "+forced+" forced and "+regional+" regional-colour pictures");return;
        }
        if(args[0].equals("validate")) {
            DiscModel disc=new DiscModel(new FileInputStream(args[1]));int count=0;
            for(DiscModel.Pgc p:disc.pgcs.values())for(long[] list:new long[][]{p.pre,p.post,p.commands})for(long cmd:list) {
                try {
                    for(int sample:new int[]{0,1,65535}) {
                        DvdVm vm=new DvdVm();java.util.Arrays.fill(vm.gprm,sample);vm.execute(cmd);
                    }
                } catch(RuntimeException e) {throw new IllegalStateException(p.key+" command "+Long.toHexString(cmd)+": "+e);}
                count++;
            }
            System.out.println("Validated "+count+" DVD commands");return;
        }
        if(args[0].equals("commands")) {
            DvdVm vm=new DvdVm();
            for(int i=1;i<args.length;i++) {
                DvdVm.Action a=vm.execute(Long.parseUnsignedLong(args[i],16));
                System.out.println(args[i]+" -> "+a+" GPRM="+java.util.Arrays.toString(vm.gprm)+" SPRM="+java.util.Arrays.toString(vm.sprm));
            }
            return;
        }
        Navigator n=new Navigator(new DiscModel(new FileInputStream(args[0])));
        n.start();
        for(int i=1;i<=args.length;i++) {
            System.out.println("STATE "+(n.pgc==null?"null":n.pgc.key)+" cell="+n.cell+" program="+n.program+" stopped="+n.stopped+" resumeMillis="+n.resumeMillis+" SPRM="+java.util.Arrays.toString(n.vm.sprm));
            if(i==args.length)break;
            if(args[i].equals("end"))n.endCell();
            else if(args[i].equals("menu"))n.rootMenu(0);
            else if(args[i].startsWith("menu-time="))n.rootMenu(Long.parseLong(args[i].substring(10)));
            else if(args[i].startsWith("title="))n.selectTitle(Integer.parseInt(args[i].substring(6)));
            else if(args[i].startsWith("button=")) {
                String[] b=args[i].substring(7).split(",");
                n.button(Long.parseUnsignedLong(b[1],16),Integer.parseInt(b[0]));
            }
        }
    }
}
