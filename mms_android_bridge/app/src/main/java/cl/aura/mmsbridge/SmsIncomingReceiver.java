package cl.aura.mmsbridge;
import android.content.*;
import android.provider.Telephony;
import android.telephony.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

public class SmsIncomingReceiver extends BroadcastReceiver {
    static String authorized(String raw){
        String n=PhoneNumberUtils.normalizeNumber(raw==null?"":raw);
        // Acepta el numero autorizado en sus distintas formas.
        if(n.equals("900000000")||n.equals("56900000000")||n.equals("0056900000000"))n="+56900000000";
        return n.equals("+56900000000")?n:null;
    }
    public void onReceive(Context c,Intent i){
        if(!Telephony.Sms.Intents.SMS_RECEIVED_ACTION.equals(i.getAction()))return;
        try{
            SmsMessage[] parts=Telephony.Sms.Intents.getMessagesFromIntent(i);
            if(parts==null||parts.length==0)return;
            String sender=authorized(parts[0].getOriginatingAddress()); if(sender==null)return;
            StringBuilder body=new StringBuilder();
            for(SmsMessage part:parts){if(!sender.equals(authorized(part.getOriginatingAddress())))return;body.append(part.getMessageBody());}
            String text=body.toString(); if(text.trim().isEmpty()||text.length()>4000)return;
            long time=parts[0].getTimestampMillis();
            byte[] hash=MessageDigest.getInstance("SHA-256").digest((sender+"\n"+time+"\n"+text).getBytes(StandardCharsets.UTF_8));
            StringBuilder id=new StringBuilder();for(byte b:hash)id.append(String.format("%02x",b&255));
            try(SmsInbox inbox=new SmsInbox(c)){inbox.add(id.toString(),sender,text,time);}
        }catch(Exception e){android.util.Log.e("AuraSMS","Cannot persist incoming SMS",e);}
    }
}
