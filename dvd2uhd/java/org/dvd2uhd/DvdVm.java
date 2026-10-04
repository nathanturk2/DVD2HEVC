/* GPL-3.0-or-later.
 * DVD instruction decoding adapted from libdvdnav src/vm/decoder.c.
 * Copyright (C) 2000, 2001 Martin Norback, Hakan Hjort; 2002-2004 dvdnav project.
 * Original code GPL-2.0-or-later. See docs/THIRD_PARTY.md.
 */
package org.dvd2uhd;

import java.util.Random;

/** The DVD 16-bit register machine, independent of BD-J or player internals. */
public final class DvdVm {
    public final int[] sprm = new int[24];
    public final int[] gprm = new int[16];
    private final boolean[] counter = new boolean[16];
    private final long[] counterStart = new long[16];
    private final Random random;
    public interface Clock {long millis();}
    private final Clock clock;
    private boolean titleDomain=true;
    private long clockAnchor,dvdElapsed,timerDeadline=-1;
    private long instruction, examined;

    public static final class Action {
        public final int op, a, b, c;
        public Action(int op, int a, int b, int c) {
            this.op=op; this.a=a; this.b=b; this.c=c;
        }
        public String toString() { return op+":"+a+":"+b+":"+c; }
    }

    public DvdVm() { this(new Random()); }
    public DvdVm(Random random) {this(random,new Clock(){public long millis(){return System.nanoTime()/1000000;}});}
    public DvdVm(Random random,Clock clock) {
        this.random=random;
        this.clock=clock;
        clockAnchor=clock.millis();
        sprm[0]=0x656e; sprm[1]=15; sprm[2]=62; sprm[3]=1;
        sprm[12]=0x5553; sprm[13]=15; sprm[14]=0x100; sprm[15]=0x7cfc;
        sprm[16]=0x656e; sprm[18]=0x656e; sprm[20]=1;
    }
    private int bits(int start, int count) {
        if(count==0) return 0;
        long mask=((1L<<count)-1) << (start-count+1);
        examined |= mask;
        return (int)((instruction & mask) >>> (start-count+1));
    }
    public int get(int reg) {
        if(reg<0 || reg>=16) throw new IllegalArgumentException("Invalid GPRM "+reg);
        return counter[reg] ? (int)((dvdMillis()-counterStart[reg])/1000)&65535 : gprm[reg];
    }
    public void set(int reg, int value) {
        gprm[reg]=value&65535;
        if(counter[reg]) counterStart[reg]=dvdMillis()-(value&65535)*1000L;
    }
    private long dvdMillis() {
        long now=clock.millis();
        if(titleDomain)dvdElapsed+=Math.max(0,now-clockAnchor);
        clockAnchor=now;return dvdElapsed;
    }
    public void titleDomain(boolean title) {dvdMillis();titleDomain=title;}
    public void clearTimer() {timerDeadline=-1;sprm[9]=0;}
    public void refreshTimer() {
        if(timerDeadline>=0)sprm[9]=(int)Math.max(0,(timerDeadline-dvdMillis()+999)/1000);
    }
    public int navigationTimerTarget() {
        refreshTimer();
        if(titleDomain && timerDeadline>=0 && sprm[9]==0){timerDeadline=-1;return sprm[10];}
        return 0;
    }
    private int reg(int n) {
        refreshTimer();
        if((n & 128)!=0) {
            if((n & 127)>=24) throw new IllegalArgumentException("Invalid SPRM "+n);
            return sprm[n&127];
        }
        return get(n&15);
    }
    private int data(int imm, int start) {
        return imm!=0 ? bits(start,16) : reg(bits(start-8,8));
    }
    private int smallData(int imm,int start) {
        return imm!=0 ? bits(start-1,7) : get(bits(start-4,4));
    }
    private boolean compare(int op,int a,int b) {
        switch(op) {
            case 1:return (a&b)!=0;
            case 2:return a==b;
            case 3:return a!=b;
            case 4:return a>=b;
            case 5:return a>b;
            case 6:return a<=b;
            case 7:return a<b;
            default:throw new IllegalArgumentException("Invalid comparison "+op);
        }
    }
    private boolean condition(int version) {
        int op=bits(54,3);
        if(op==0) return true;
        switch(version) {
            case 1:return compare(op,reg(bits(39,8)),data(bits(55,1),31));
            case 2:return compare(op,reg(bits(15,8)),reg(bits(7,8)));
            case 3:return compare(op,reg(bits(47,8)),data(bits(55,1),15));
            case 4:return compare(op,reg(bits(51,4)),data(bits(55,1),31));
            default:throw new IllegalArgumentException("Invalid condition encoding");
        }
    }
    private Action sublink(boolean cond) {
        int button=bits(15,6), op=bits(4,5);
        if(op>16 || op==4 || op==8 || op==14 || op==15)
            throw new IllegalArgumentException("Reserved LinkSIns "+op);
        return cond ? new Action(op,button,0,0) : null;
    }
    private Action link(boolean cond) {
        int op=bits(51,4);
        Action a;
        switch(op) {
            case 0:return null;
            case 1:return sublink(cond);
            case 4:a=new Action(17,bits(14,15),0,0);break;
            case 5:a=new Action(18,bits(9,10),bits(15,6),0);break;
            case 6:a=new Action(19,bits(6,7),bits(15,6),0);break;
            case 7:a=new Action(20,bits(7,8),bits(15,6),0);break;
            default:throw new IllegalArgumentException("Reserved link opcode "+op);
        }
        return cond ? a : null;
    }
    private Action jump(boolean cond) {
        Action a;
        switch(bits(51,4)) {
            case 1:a=new Action(21,0,0,0);break;
            case 2:a=new Action(22,bits(22,7),0,0);break;
            case 3:a=new Action(23,bits(22,7),0,0);break;
            case 5:a=new Action(24,bits(22,7),bits(41,10),0);break;
            case 6:
                switch(bits(23,2)) {
                    case 0:a=new Action(25,0,0,0);break;
                    case 1:a=new Action(26,bits(19,4),0,0);break;
                    case 2:a=new Action(27,bits(31,8),bits(39,8),bits(19,4));break;
                    default:a=new Action(28,bits(46,15),0,0);
                }
                break;
            case 8:
                switch(bits(23,2)) {
                    case 0:a=new Action(29,bits(31,8),0,0);break;
                    case 1:a=new Action(30,bits(19,4),bits(31,8),0);break;
                    case 2:a=new Action(31,bits(19,4),bits(31,8),0);break;
                    default:a=new Action(32,bits(46,15),bits(31,8),0);
                }
                break;
            default:throw new IllegalArgumentException("Reserved jump opcode");
        }
        return cond ? a : null;
    }
    private void operation(int op,int r,int r2,int val) {
        int old=get(r);
        switch(op) {
            case 0:return;
            case 1:set(r,val);return;
            case 2:set(r2,old);set(r,val);return;
            case 3:set(r,Math.min(65535,old+val));return;
            case 4:set(r,Math.max(0,old-val));return;
            case 5:set(r,(int)Math.min(65535L,(long)old*val));return;
            case 6:set(r,val==0 ? 65535 : old/val);return;
            case 7:set(r,val==0 ? 65535 : old%val);return;
            case 8:set(r,val==0 ? 1 : 1+random.nextInt(val));return;
            case 9:set(r,old&val);return;
            case 10:set(r,old|val);return;
            case 11:set(r,old^val);return;
            default:throw new IllegalArgumentException("Reserved set operation "+op);
        }
    }
    private void setInstruction(int version,boolean cond) {
        int op=bits(59,4), r=bits(version==1?35:51,4), r2=bits(version==1?19:35,4);
        int val=data(bits(60,1),version==1?31:47);
        if(cond) operation(op,r,r2,val);
    }
    private Action system(boolean cond) {
        switch(bits(59,4)) {
            case 1:
                for(int i=1;i<=3;i++) {
                    if(bits(63-(2+i)*8,1)!=0) {
                        int val=smallData(bits(60,1),47-i*8);
                        if(cond) sprm[i]=val;
                    }
                }
                break;
            case 2:
                int timer=data(bits(60,1),47), pgc=bits(23,8);
                if(cond && titleDomain) {
                    sprm[9]=timer;sprm[10]=pgc;
                    timerDeadline=timer==0?-1:dvdMillis()+timer*1000L;
                }
                break;
            case 3:
                int val=data(bits(60,1),47), n=bits(19,4), mode=bits(23,1);
                // libdvdnav changes mode even when the conditional assignment
                // is false; retain that behavior for converted DVD commands.
                counter[n]=mode!=0;
                if(cond)set(n,val);
                break;
            case 6:
                int button=data(bits(60,1),31);
                if(cond) sprm[8]=button;
                break;
            default:throw new IllegalArgumentException("Reserved system-set opcode");
        }
        return link(cond);
    }
    /** Action -1 is a one-based goto, -2 is Break; other numbers match libdvdnav. */
    public Action execute(long cmd) {
        instruction=cmd; examined=0;
        Action result=null;
        boolean c;
        switch(bits(63,3)) {
            case 0:
                c=condition(1);
                switch(bits(51,4)) {
                    case 0:break;
                    case 1:
                        int line=bits(7,8);
                        if(c) result=new Action(-1,line,0,0);
                        break;
                    case 2:if(c) result=new Action(-2,0,0,0);break;
                    case 3:
                        int target=bits(7,8), level=bits(11,4);
                        if(c) { sprm[13]=level; result=new Action(-1,target,0,0); }
                        break;
                    default:throw new IllegalArgumentException("Reserved special opcode");
                }
                break;
            case 1:result=bits(60,1)!=0 ? jump(condition(2)) : link(condition(1));break;
            case 2:result=system(condition(2));break;
            case 3:c=condition(3);setInstruction(1,c);result=link(c);break;
            case 4:setInstruction(2,true);result=sublink(condition(4));break;
            case 5:c=condition(4);setInstruction(2,c);result=sublink(c);break;
            case 6:c=condition(4);setInstruction(2,c);result=sublink(true);break;
            default:throw new IllegalArgumentException("Reserved command type 7");
        }
        if((instruction & ~examined)!=0)
            throw new IllegalArgumentException("Unrecognized nonzero DVD command bits: "+Long.toHexString(instruction&~examined));
        return result;
    }
    public Action run(long[] commands) {
        int line=0,steps=0;
        while(line<commands.length) {
            if(++steps>10000) throw new IllegalStateException("DVD command loop exceeds 10000 instructions");
            Action a=execute(commands[line]);
            if(a==null) line++;
            else if(a.op==-2) return null;
            else if(a.op==-1) {
                if(a.a<1 || a.a>commands.length) throw new IllegalArgumentException("DVD goto outside command table");
                line=a.a-1;
            } else return a;
        }
        return null;
    }
}
