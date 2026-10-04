// SPDX-License-Identifier: GPL-3.0-or-later
package org.dvd2uhd;

/** Original SPU lifetime across consecutive cells; seeks discard carried state. */
public final class SubpictureTimeline {
    public static final class Active {
        public final DiscModel.Picture picture;
        public final long offset;
        Active(DiscModel.Picture picture,long offset){this.picture=picture;this.offset=offset;}
        boolean visible(long time){return time+offset>=picture.start && time+offset<picture.end;}
    }
    private DiscModel.Graphics graphics;
    private Active[] carry=new Active[32];
    public void enter(DiscModel.Graphics next,long elapsed,boolean consecutive) {
        Active[] retained=new Active[32];
        if(consecutive)for(int stream=0;stream<32;stream++) {
            // Test just before the boundary: a new stream reset exactly at the
            // boundary belongs to the next cell and supersedes this carry there.
            Active active=active(stream,Math.max(0,elapsed-1));
            if(active!=null && active.visible(elapsed))retained[stream]=new Active(active.picture,active.offset+elapsed);
        }
        carry=retained;graphics=next;
    }
    public void discardCarry(){carry=new Active[32];}
    public Active active(int stream,long time) {
        if(graphics==null || stream<0 || stream>=32)return null;
        DiscModel.Picture p=graphics.picture(stream,time);
        if(p!=null)return new Active(p,0);
        Active old=carry[stream];
        return graphics.latestReset(stream,time)==Long.MIN_VALUE && old!=null && old.visible(time)?old:null;
    }
}
