/* SPDX-License-Identifier: MIT */
#include "transcript_filter.h"
#include <errno.h>
#include <stdio.h>
#include <string.h>

enum { TEXT, ESCAPE, APC, DCS_HEADER, PASS_STRING, PASS_ESCAPE, GRAPHICS, GRAPHICS_ESCAPE };
/* C1 bytes inside a UTF-8 scalar are ordinary text, even across read splits. */
static bool utf8_tail(BtTranscriptFilter *f, uint8_t c) {
    if(f->utf8_continuations && c>=f->utf8_min && c<=f->utf8_max) {
        --f->utf8_continuations;
        f->utf8_min=0x80; f->utf8_max=0xbf;
        return true;
    }
    f->utf8_continuations=c>=0xc2 && c<=0xdf?1:c>=0xe0 && c<=0xef?2:c>=0xf0 && c<=0xf4?3:0;
    f->utf8_min=c==0xe0?0xa0:c==0xf0?0x90:0x80;
    f->utf8_max=c==0xed?0x9f:c==0xf4?0x8f:0xbf;
    return false;
}
static void add(uint64_t *value, uint64_t n) { *value=UINT64_MAX-*value<n?UINT64_MAX:*value+n; }
static int emit(BtTranscriptFilter *f, const void *data, size_t n) {
    if(!n) return 0;
    if(f->sink(f->userdata,data,n)) { f->failed=true; return -1; }
    add(&f->output_bytes,n); return 0;
}
static int marker(BtTranscriptFilter *f, bool incomplete) {
    char text[128];
    int n=snprintf(text,sizeof(text),"\r\n[batty: %llu graphics bytes elided%s]\r\n",
        (unsigned long long)f->pending,incomplete?", incomplete":"");
    if(emit(f,text,(size_t)n)) return -1;
    add(&f->elided_bytes,f->pending); add(&f->graphics_sequences,1);
    f->pending=0; f->state=TEXT; return 0;
}
void bt_transcript_filter_init(BtTranscriptFilter *f, bool keep, BtTranscriptSink sink, void *userdata) {
    memset(f,0,sizeof(*f)); f->keep=keep; f->sink=sink; f->userdata=userdata;
}
int bt_transcript_filter_write(BtTranscriptFilter *f, const void *data, size_t length) {
    if(!f || !f->sink || (!data && length) || f->failed || f->finished) { errno=EINVAL; return -1; }
    const uint8_t *bytes=data;
    add(&f->input_bytes,length);
    if(f->keep) return emit(f,data,length);
    size_t at=0;
    while(at<length) {
        uint8_t c=bytes[at];
        if(f->state==TEXT) {
            size_t start=at;
            while(at<length) {
                c=bytes[at];
                if(utf8_tail(f,c)) { ++at; continue; }
                if(c==27 || c==0x9f || c==0x90 || c==0x9d || c==0x9e || c==0x98) break;
                ++at;
            }
            if(emit(f,bytes+start,at-start)) return -1;
            if(at<length) {
                c=bytes[at++]; f->header[0]=c; f->header_length=1;
                if(c==27) f->state=ESCAPE;
                else if(c==0x9f) f->state=APC;
                else if(c==0x90) f->state=DCS_HEADER;
                else { if(emit(f,f->header,1)) return -1; f->header_length=0; f->state=PASS_STRING; }
            }
            continue;
        }
        ++at;
        if(f->state==ESCAPE) {
            if(c==']' || c=='^' || c=='X') {
                if(emit(f,f->header,f->header_length) || emit(f,&c,1)) return -1;
                f->header_length=0; f->state=PASS_STRING;
            } else if(c=='_' || c=='P') {
                f->header[f->header_length++]=c; f->state=c=='_'?APC:DCS_HEADER;
            } else {
                if(emit(f,f->header,f->header_length)) return -1;
                f->header_length=0; f->state=TEXT;
                if(c==27) { f->header[0]=27; f->header_length=1; f->state=ESCAPE; }
                else if(emit(f,&c,1)) return -1;
            }
        } else if(f->state==APC) {
            if(c=='G') { f->pending=f->header_length+1; f->header_length=0; f->state=GRAPHICS; }
            else {
                if(emit(f,f->header,f->header_length) || emit(f,&c,1)) return -1;
                f->header_length=0; f->state=c==27?PASS_ESCAPE:(c==7 || c==24 || c==26 || c==0x9c)?TEXT:PASS_STRING;
                if(f->state==PASS_STRING) utf8_tail(f,c);
            }
        } else if(f->state==DCS_HEADER) {
            if(f->header_length==sizeof(f->header)) {
                f->pending=f->header_length+1; f->header_length=0;
                f->state=c==27?GRAPHICS_ESCAPE:GRAPHICS;
                if(c==7 || c==24 || c==26 || c==0x9c) { if(marker(f,false)) return -1; }
            } else {
                f->header[f->header_length++]=c;
                if(c=='q') { f->pending=f->header_length; f->header_length=0; f->state=GRAPHICS; }
                else if(!((c>='0' && c<='9') || c==';' || c==':')) {
                    if(emit(f,f->header,f->header_length)) return -1;
                    f->header_length=0;
                    f->state=c==27?PASS_ESCAPE:(c==7 || c==24 || c==26 || c==0x9c)?TEXT:PASS_STRING;
                    if(f->state==PASS_STRING) utf8_tail(f,c);
                }
            }
        } else if(f->state==GRAPHICS || f->state==GRAPHICS_ESCAPE) {
            bool end=c==7 || c==24 || c==26 || c==0x9c || (f->state==GRAPHICS_ESCAPE && c=='\\');
            add(&f->pending,1);
            if(end) { if(marker(f,false)) return -1; }
            else f->state=c==27?GRAPHICS_ESCAPE:GRAPHICS;
        } else {
            bool tail=utf8_tail(f,c);
            bool end=c==7 || c==24 || c==26 || (c==0x9c && !tail) || (f->state==PASS_ESCAPE && c=='\\');
            if(emit(f,&c,1)) return -1;
            f->state=end?TEXT:c==27?PASS_ESCAPE:PASS_STRING;
        }
    }
    return 0;
}
int bt_transcript_filter_finish(BtTranscriptFilter *f) {
    if(!f || !f->sink || f->failed) { errno=EINVAL; return -1; }
    if(f->finished) return 0;
    if(f->state==GRAPHICS || f->state==GRAPHICS_ESCAPE) {
        if(marker(f,true)) return -1;
    } else if(emit(f,f->header,f->header_length)) return -1;
    f->header_length=0; f->finished=true; return 0;
}
