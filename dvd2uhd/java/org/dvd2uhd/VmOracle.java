// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;
import java.io.*;

/** Line protocol shared with the external libdvdnav comparison harness. */
public final class VmOracle {
    public static void main(String[] args)throws Exception {
        BufferedReader input=new BufferedReader(new InputStreamReader(System.in,"ASCII"));String line;
        while((line=input.readLine())!=null) {
            String[] parts=line.trim().split(" +");int seed=Integer.parseInt(parts[1]);DvdVm vm=new DvdVm();
            for(int i=0;i<16;i++)vm.set(i,(seed+7919*i)&65535);
            for(int i=0;i<24;i++)vm.sprm[i]=(seed+3571*i)&65535;
            try {
                DvdVm.Action a=vm.execute(Long.parseUnsignedLong(parts[0],16));
                if(a==null || a.op<0)a=new DvdVm.Action(0,0,0,0);
                StringBuilder out=new StringBuilder(a.op+" "+a.a+" "+a.b+" "+a.c);
                for(int i=0;i<16;i++)out.append(' ').append(vm.get(i));
                for(int v:vm.sprm)out.append(' ').append(v);
                System.out.println(out);
            } catch(RuntimeException e){System.out.println("REJECT "+e.getMessage());}
        }
    }
}
