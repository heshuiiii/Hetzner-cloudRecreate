# Hetzner-cloudRecreate
检查hzc服务器达量后删除重建服务器


## 1月9日更新
 - neo版本会在指定时间内检测并重建服务器  并且把对应服务器获得的ip提交给vertex下载器更新防止ip变化后无法应用到vt的规则  重启vt需要手动修改cookies


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
   "id": 103101821,   ## 这个是ssk_keys的id  要填到.env的 HETZNER_SSH_KEYS 里
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
## docker容器 
```
services:
  hetzner-cloudrecreate:
    image: aksviolet/hetzner-cloudrecreate:latest
    container_name: hetzner-cloudrecreate
    restart: unless-stopped
    volumes:
      - ./hetzner-cloudrecreate/hetzner_monitor.log:/app/hetzner_monitor.log
      - ./hetzner-cloudrecreate/.env:/app/.env

    environment:
      - TZ=Asia/Shanghai
```


可以走推荐链接注册  https://hetzner.cloud/?ref=PewRJ60CJHxt    
拿到新人20欧奖励后再使用优惠码  **LTT25** 获得额外20欧








