/* SPDX-License-Identifier: MIT */
#include "transcript_filter.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { char bytes[8192]; size_t used; bool fail; } Output;
static void require(bool ok, const char *message) {
    if(!ok) { fprintf(stderr,"FAIL transcript filter: %s\n",message); exit(1); }
}
static int sink(void *data, const void *bytes, size_t n) {
    Output *out=data;
    if(out->fail) return -1;
    require(n<sizeof(out->bytes)-out->used,"bounded fixture output");
    memcpy(out->bytes+out->used,bytes,n); out->used+=n; out->bytes[out->used]=0; return 0;
}
static void sample(const char *input, const char *expected, unsigned graphics) {
    size_t n=strlen(input);
    for(size_t split=0;split<=n;++split) for(unsigned single=0;single<2;++single) {
        Output out={0}; BtTranscriptFilter f;
        bt_transcript_filter_init(&f,false,sink,&out);
        require(!bt_transcript_filter_write(&f,input,split),"first chunk");
        for(size_t at=split;at<n;) {
            size_t count=single?1:n-at;
            require(!bt_transcript_filter_write(&f,input+at,count),"remaining chunk"); at+=count;
        }
        require(!bt_transcript_filter_finish(&f) && !bt_transcript_filter_finish(&f),"finish is idempotent");
        require(!strcmp(out.bytes,expected),"chunk boundaries preserve exact filtered stream");
        require(f.input_bytes==n && f.output_bytes==out.used && f.graphics_sequences==graphics,"stream counters");
        require(bt_transcript_filter_write(&f,"x",1)==-1,"reject writes after finish");
    }
}
int main(void) {
    sample("hello\033[31mred\033[0m café 日本語\n","hello\033[31mred\033[0m café 日本語\n",0);
    sample("a\033_Gabc\033\\b","a\r\n[batty: 8 graphics bytes elided]\r\nb",1);
    sample("a\x9f" "Gabc\x9c" "b","a\r\n[batty: 6 graphics bytes elided]\r\nb",1);
    sample("\x9f" "Gx\033\\","\r\n[batty: 5 graphics bytes elided]\r\n",1);
    sample("\033P0;1qDATA\033\\","\r\n[batty: 12 graphics bytes elided]\r\n",1);
    sample("\x90" "0;1qDATA\x9c","\r\n[batty: 10 graphics bytes elided]\r\n",1);
    sample("\033_Gx\aafter","\r\n[batty: 5 graphics bytes elided]\r\nafter",1);
    sample("\033_Gx\030after","\r\n[batty: 5 graphics bytes elided]\r\nafter",1);
    sample("\033_Gabc","\r\n[batty: 6 graphics bytes elided, incomplete]\r\n",1);
    sample("end\033_","end\033_",0);
    sample("end\033P12;","end\033P12;",0);
    sample("\033\033_Gx\033\\","\033\r\n[batty: 6 graphics bytes elided]\r\n",1);
    sample("\033P$q\033_Gliteral\033\\end","\033P$q\033_Gliteral\033\\end",0);
    sample("\033_nonimage\033_Gliteral\033\\end","\033_nonimage\033_Gliteral\033\\end",0);
    sample("\033]0;title \033_Gliteral\aend","\033]0;title \033_Gliteral\aend",0);
    sample("\x9d" "title \xe2\x98\x9f \x9f" "Gliteral\x9c" "end",
           "\x9d" "title \xe2\x98\x9f \x9f" "Gliteral\x9c" "end",0);
    sample("\xe2\x98\x9f" "text", "\xe2\x98\x9f" "text",0);
    sample("\xe0\x80\x9f" "Gx\x9c", "\xe0\x80\r\n[batty: 4 graphics bytes elided]\r\n",1);
    sample("\x9e" "note\xc2\x9c" "still\x9c" "\x9f" "Gx\x9c",
           "\x9e" "note\xc2\x9c" "still\x9c\r\n[batty: 4 graphics bytes elided]\r\n",1);
    sample("\033_\a\033_Gx\033\\","\033_\a\r\n[batty: 6 graphics bytes elided]\r\n",1);
    char oversized[100]; oversized[0]=27; oversized[1]='P';
    memset(oversized+2,'1',80); memcpy(oversized+82,"qDATA\033\\",8);
    sample(oversized,"\r\n[batty: 89 graphics bytes elided]\r\n",1);
    Output out={0}; BtTranscriptFilter f;
    bt_transcript_filter_init(&f,false,sink,&out);
    require(!bt_transcript_filter_write(&f,"\033_G",3),"large image prefix");
    char payload[4096]; memset(payload,'A',sizeof(payload));
    for(unsigned i=0;i<4096;++i) require(!bt_transcript_filter_write(&f,payload,sizeof(payload)),"large payload streaming");
    require(!out.used,"payload not buffered in output");
    require(!bt_transcript_filter_write(&f,"\033\\",2) && !bt_transcript_filter_finish(&f),"large payload terminator");
    require(f.elided_bytes==16777221 && out.used<100,"16 MiB graphics produces a bounded marker");
    memset(&out,0,sizeof(out)); bt_transcript_filter_init(&f,true,sink,&out);
    const char raw[]="\033_Gabc\033\\\0binary";
    require(!bt_transcript_filter_write(&f,raw,sizeof(raw)) && !bt_transcript_filter_finish(&f) &&
        out.used==sizeof(raw) && !memcmp(out.bytes,raw,sizeof(raw)),"keep policy preserves binary bytes");
    memset(&out,0,sizeof(out)); out.fail=true; bt_transcript_filter_init(&f,false,sink,&out);
    require(bt_transcript_filter_write(&f,"text",4)==-1 && f.failed &&
        bt_transcript_filter_finish(&f)==-1,"sink failure is latched");
    puts("PASS transcript filter: graphics elision, split reads, binary keep, incomplete streams, bounded memory and sink failure");
    return 0;
}
