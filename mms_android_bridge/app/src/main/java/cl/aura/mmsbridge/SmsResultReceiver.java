package cl.aura.mmsbridge;
import android.app.Activity;
import android.content.*;
import org.json.JSONObject;
public class SmsResultReceiver extends BroadcastReceiver {
    public void onReceive(Context c, Intent i) {
        c=BootStorage.context(c);
        try {
            String id=i.getStringExtra("id");
            if(id==null)return;
            android.content.SharedPreferences p=c.getSharedPreferences("status",0);
            JSONObject state=new JSONObject(p.getString(id,"{}"));
            boolean delivery=i.getAction()!=null && i.getAction().startsWith("delivered.");
            if (!"delivered".equals(state.optString("state")) && !"failed".equals(state.optString("state"))) {
                String stage=delivery?"delivery":"sent";
                int part=i.getIntExtra("part",0),total=i.getIntExtra("total",1);
                state.put(stage+part,getResultCode()==Activity.RESULT_OK);
                boolean all=true;for(int n=0;n<total;n++)all=all&&state.optBoolean(stage+n,false);
                if(getResultCode()!=Activity.RESULT_OK)state.put("state","failed");
                else if(all)state.put("state",delivery?"delivered":"sent");
                state.put("result_code",getResultCode());
                p.edit().putString(id,state.toString()).commit();
            }
        } catch(Exception ignored) {}
    }
}
