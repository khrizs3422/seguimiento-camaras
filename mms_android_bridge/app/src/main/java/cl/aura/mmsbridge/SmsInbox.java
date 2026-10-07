package cl.aura.mmsbridge;
import android.content.*;
import android.database.Cursor;
import android.database.sqlite.*;
import org.json.*;

public class SmsInbox extends SQLiteOpenHelper {
    public SmsInbox(Context c){super(BootStorage.context(c),"inbox.db",null,1);}
    public void onCreate(SQLiteDatabase db){db.execSQL("CREATE TABLE inbox(id TEXT PRIMARY KEY,sender TEXT,body TEXT,received INTEGER,acked INTEGER DEFAULT 0)");}
    public void onUpgrade(SQLiteDatabase db,int a,int b){}
    public void add(String id,String sender,String body,long received){
        ContentValues v=new ContentValues();v.put("id",id);v.put("sender",sender);v.put("body",body);v.put("received",received);
        getWritableDatabase().insertWithOnConflict("inbox",null,v,SQLiteDatabase.CONFLICT_IGNORE);
        getWritableDatabase().delete("inbox","acked=1 AND received<?",new String[]{Long.toString(System.currentTimeMillis()-7L*86400000)});
    }
    public JSONArray pending() throws Exception {
        JSONArray rows=new JSONArray();
        try(Cursor c=getReadableDatabase().rawQuery("SELECT id,sender,body,received FROM inbox WHERE acked=0 ORDER BY received LIMIT 20",null)){
            while(c.moveToNext())rows.put(new JSONObject().put("id",c.getString(0)).put("sender",c.getString(1)).put("text",c.getString(2)).put("received",c.getLong(3)));
        }
        return rows;
    }
    public void ack(String id){ContentValues v=new ContentValues();v.put("acked",1);getWritableDatabase().update("inbox",v,"id=?",new String[]{id});}
}
