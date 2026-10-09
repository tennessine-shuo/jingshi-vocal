
var chartCompare=echarts.init(document.getElementById('chartCompare'));
chartCompare.setOption({
  grid:{left:60,right:30,top:40,bottom:30},
  tooltip:{trigger:'axis',axisPointer:{type:'shadow'}},
  legend:{top:0,textStyle:{color:'#5c7089',fontSize:11}},
  xAxis:{type:'category',data:L.map(function(l){return l.lesson;}),
    axisLabel:{color:'#1c2b3a',fontSize:12},axisLine:{lineStyle:{color:'#b9cdea'}}},
  yAxis:{type:'value',max:100,axisLabel:{color:'#5c7089',fontSize:11},splitLine:{lineStyle:{color:'#edf2f9'}}},
  series:(function(){
    var s=[
      {name:'老师授课分',type:'bar',data:L.map(function(l){return l.teach_score;}),barWidth:26,
       itemStyle:{color:'#2f6fdb',borderRadius:[5,5,0,0]},
       label:{show:true,position:'top',color:'#2f6fdb'}},
      {name:'课堂活跃分',type:'bar',data:L.map(function(l){return l.active_score;}),barWidth:26,
       itemStyle:{color:'#0e9f8a',borderRadius:[5,5,0,0]},
       label:{show:true,position:'top',color:'#0e9f8a'}}
    ];
    if(L[0]&&L[0].focus_score!=null){
      s.push({name:'学生专注分',type:'bar',data:L.map(function(l){return l.focus_score;}),barWidth:26,
       itemStyle:{color:'#f5a623',borderRadius:[5,5,0,0]},
       label:{show:true,position:'top',color:'#c77d12'}});
    }
    return s;
  })()
});
var chartSeg=echarts.init(document.getElementById('chartSeg'));
var segNames=['前 1/3','中 1/3','后 1/3'];
chartSeg.setOption({
  grid:{left:60,right:30,top:40,bottom:30},
  tooltip:{trigger:'axis',axisPointer:{type:'shadow'},formatter:function(ps){
    var s=ps[0].axisValue+'<br/>';ps.forEach(function(p){s+=p.marker+p.seriesName+'：'+p.value+'%<br/>';});return s;}},
  legend:{top:0,textStyle:{color:'#5c7089',fontSize:11}},
  xAxis:{type:'category',data:segNames,axisLabel:{color:'#1c2b3a',fontSize:12},axisLine:{lineStyle:{color:'#b9cdea'}}},
  yAxis:{type:'value',axisLabel:{formatter:'{value}%',color:'#5c7089',fontSize:11},splitLine:{lineStyle:{color:'#edf2f9'}}},
  series:L.map(function(l,i){
    return {name:l.lesson,type:'line',data:l.seg_ratio,smooth:true,symbolSize:8,
      lineStyle:{width:3,color:['#2f6fdb','#0e9f8a','#f5a623','#8e6fd8'][i%4]},
      itemStyle:{color:['#2f6fdb','#0e9f8a','#f5a623','#8e6fd8'][i%4]},
      label:{show:true,formatter:'{c}%',color:'#5c7089',fontSize:10}};
  })
});
L.forEach(function(l,idx){
  var el1=document.getElementById('seg'+idx);
  if(el1){
    var c=echarts.init(el1);
    c.setOption({
      grid:{left:40,right:20,top:30,bottom:24},
      tooltip:{trigger:'axis',axisPointer:{type:'shadow'}},
      xAxis:{type:'category',data:segNames,axisLabel:{color:'#5c7089',fontSize:10},axisLine:{lineStyle:{color:'#b9cdea'}}},
      yAxis:{type:'value',axisLabel:{formatter:'{value}%',color:'#5c7089',fontSize:10},splitLine:{lineStyle:{color:'#edf2f9'}}},
      series:[{type:'bar',data:l.seg_ratio,barWidth:36,
        itemStyle:{color:function(p){return ['#2f6fdb','#0e9f8a','#f5a623'][p.dataIndex];},borderRadius:[4,4,0,0]},
        label:{show:true,position:'top',formatter:'{c}%',color:'#5c7089',fontSize:10}}]
    });
  }
  var el2=document.getElementById('ring'+idx);
  if(el2){
    var c2=echarts.init(el2);
    c2.setOption({
      tooltip:{formatter:'{b}：{c}%'},
      series:[{type:'pie',radius:['45%','72%'],data:[
        {value:l.teach_score,name:'授课分'},
        {value:l.active_score,name:'活跃分'},
        {value:100-Math.max(l.teach_score,l.active_score),name:'待提升',itemStyle:{color:'#e8eef7'}}
      ],label:{fontSize:11,color:'#5c7089'},itemStyle:{borderRadius:4}}]
    });
  }
});
