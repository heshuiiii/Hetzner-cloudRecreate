# Hetzner-cloudRecreate
检查hzc服务器达量后删除重建服务器


在运行脚本前确保你创建了对应的服务器 设置了IP保护 ssh_key的id填写正确




# 创建.env 环境文件 内容请填写完整   telegram参数也是必填

### apikey 在  Projects -  Security  -  API tokens  
### 获取ssh_keys的id值
```
curl \
	-H "Authorization: Bearer $API_TOKEN" \
	"https://api.hetzner.cloud/v1/ssh_keys"
```
```
{
 "ssh_keys": [
  {
   "id": 103101821,   ## 这个是ssk_keys的id  要填到下面 HETZNER_SSH_KEYS 里
   "name": "hetzner-key",
   "public_key": "",
   "fingerprint": "",
   "labels": {},
   "created": ""
  }
 ],
 "meta": {
  "pagination": {
   "last_page": 1,
   "next_page": null,
   "page": 1,
   "per_page": 25,
   "previous_page": null,
   "total_entries": 1
  }
 }
}
```

  
```
API_KEY = 
HETZNER_SSH_KEYS=
TRAFFIC_THRESHOLD = 0.85  # 80% 流量阈值
CHECK_INTERVAL = 1800  # 每小时检查一次
```
```
TELEGRAM_BOT_TOKEN = 
TELEGRAM_CHAT_ID = 
```


可以走推荐链接注册  https://hetzner.cloud/?ref=PewRJ60CJHxt    
拿到新人20欧奖励后再使用优惠码  **LTT25** 获得额外20欧



